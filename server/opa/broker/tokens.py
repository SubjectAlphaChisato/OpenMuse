"""Provider-neutral token handling for the broker: single-flight refresh under a row lock (CON-01).

Tokens without an expiry (e.g. GitHub App tokens with expiry disabled) are used as-is and refreshed only after
a 401, if a refresh token exists. A revoked grant marks the connection for reconnection.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from opa.db.models import BrokerCredential
from opa.sdk.contract import ConnectorError

REFRESH_MARGIN = timedelta(seconds=60)


class InvalidGrant(Exception):
    """The refresh token was revoked or expired; the owner must reconnect."""


class ReauthRequired(ConnectorError):
    def __init__(self, label: str = "the account") -> None:
        super().__init__("auth_required", f"Access to {label} was revoked or expired; reconnect the account")


def secret_from_token(tok: dict[str, Any], now: datetime, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    secret = dict(previous or {})
    secret["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        secret["refresh_token"] = tok["refresh_token"]
    secret["expires_at"] = (now + timedelta(seconds=int(tok["expires_in"]))).isoformat() if tok.get("expires_in") else None
    if tok.get("scope"):
        secret["scopes"] = sorted(set(str(tok["scope"]).replace(",", " ").split()))
    return secret


class TokenManager:
    def __init__(self, sm, vault, clock, connection_id: str, oauth, *, label: str = "the account", on_reauth=None) -> None:
        self.sm, self.vault, self.clock = sm, vault, clock
        self.connection_id, self.oauth, self.label, self.on_reauth = connection_id, oauth, label, on_reauth

    async def access_token(self, *, stale: str | None = None) -> str:
        """A valid access token. Concurrent callers wait on the row lock, so an expired token is refreshed once."""
        async with self.sm() as db, db.begin():
            row = await db.scalar(select(BrokerCredential).where(BrokerCredential.connection_id == self.connection_id).with_for_update())
            if row is None:
                raise ConnectorError("auth_required", f"no stored credential for {self.label}; reconnect the account")
            secret = self.vault.decrypt(row.dek_wrapped, row.secret_enc, self.connection_id)
            expires = datetime.fromisoformat(secret["expires_at"]) if secret.get("expires_at") else None
            fresh = expires is None or expires - REFRESH_MARGIN > self.clock.now()
            if fresh and (stale is None or secret["access_token"] != stale):
                return secret["access_token"]
            if not secret.get("refresh_token"):
                reauth = True
            else:
                try:
                    tok = await self.oauth.refresh(secret["refresh_token"])
                except InvalidGrant:
                    reauth = True
                else:
                    reauth = False
                    secret = secret_from_token(tok, self.clock.now(), secret)
                    row.dek_wrapped, row.secret_enc = self.vault.encrypt(secret, self.connection_id)
                    row.access_expires_at = datetime.fromisoformat(secret["expires_at"]) if secret.get("expires_at") else None
                    return secret["access_token"]
        if reauth:
            if self.on_reauth:
                await self.on_reauth(self.connection_id)
            raise ReauthRequired(self.label)
        raise AssertionError("unreachable")
