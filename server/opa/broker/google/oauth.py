"""Broker-side Google OAuth: code exchange, refresh, revocation, identity (CON-01).

Only the broker holds the client secret and tokens. Uses Authlib's httpx client.
"""
from __future__ import annotations

from typing import Any

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

from opa.core.settings import Settings
from opa.sdk.contract import ConnectorError


class InvalidGrant(Exception):
    """The refresh token was revoked or expired; the owner must reconnect."""


def _error_code(exc: Exception) -> str | None:
    return getattr(exc, "error", None)


class GoogleOAuth:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.google_client_id or not settings.google_client_secret:
            raise ConnectorError("auth_required", "Google OAuth is not configured (OPA_GOOGLE_CLIENT_ID / OPA_GOOGLE_CLIENT_SECRET)")
        self.s = settings
        self.transport = transport

    def _client(self, **kw: Any) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            client_id=self.s.google_client_id, client_secret=self.s.google_client_secret,
            token_endpoint_auth_method="client_secret_post", transport=self.transport, timeout=30, **kw,
        )

    async def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
        async with self._client(redirect_uri=redirect_uri) as c:
            try:
                token = await c.fetch_token(self.s.google_token_url, grant_type="authorization_code", code=code, code_verifier=code_verifier)
            except httpx.TransportError:
                raise ConnectorError("provider_unavailable", "could not reach Google") from None
            except Exception as e:  # Authlib raises OAuthError subclasses carrying `.error`
                raise ConnectorError("auth_required", f"Google rejected the sign-in ({_error_code(e) or 'error'})") from None
        if not token.get("refresh_token"):
            raise ConnectorError("auth_required", "Google did not return a refresh token; remove the app's access in your Google account and connect again")
        return dict(token)

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        async with self._client() as c:
            try:
                return dict(await c.refresh_token(self.s.google_token_url, refresh_token=refresh_token))
            except httpx.TransportError:
                raise ConnectorError("provider_unavailable", "could not reach Google to refresh access") from None
            except Exception as e:
                if _error_code(e) in ("invalid_grant", "unauthorized_client"):
                    raise InvalidGrant() from None
                raise ConnectorError("provider_unavailable", f"token refresh failed ({_error_code(e) or 'error'})") from None

    async def revoke(self, token: str) -> tuple[bool, str | None]:
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=15) as c:
                r = await c.post(self.s.google_revoke_url, data={"token": token}, headers={"content-type": "application/x-www-form-urlencoded"})
        except httpx.TransportError:
            return False, "could not reach Google to revoke access"
        if r.status_code == 200:
            return True, None
        return False, f"Google answered {r.status_code} to the revocation request"

    async def identity(self, access_token: str) -> dict[str, str]:
        async with httpx.AsyncClient(transport=self.transport, timeout=15) as c:
            r = await c.get(self.s.google_userinfo_url, headers={"authorization": f"Bearer {access_token}"})
        if r.status_code != 200:
            raise ConnectorError("auth_required", "could not read the Google account identity")
        data = r.json()
        if not data.get("sub") or not data.get("email"):
            raise ConnectorError("auth_required", "Google did not return the account email; the email scope is required")
        return {"sub": str(data["sub"]), "email": str(data["email"]).lower()}
