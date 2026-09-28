"""Google OAuth authorization-code flow with PKCE (CON-01). The API never sees tokens."""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode

from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.common.security import generate_token
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from opa.api.deps import ClockDep, DbDep, OwnerDep, SessionDep, SettingsDep, get_owned
from opa.broker.google import scopes as gscopes
from opa.core.canonical import sha256_hex
from opa.core.errors import Invalid
from opa.core.ids import new_id
from opa.db.models import AuditLog, Connection, OAuthState
from opa.db.outbox import emit
from opa.sdk.contract import ConnectorError

router = APIRouter(prefix="/api", tags=["oauth"])
Mode = Literal["off", "read_only", "read_write"]


class AuthorizeIn(BaseModel):
    connection_id: str | None = None  # set for reconnect or to add scopes to an existing connection
    modes: dict[str, Mode] = {"gmail": "read_only", "calendar": "read_only"}


class AuthorizeOut(BaseModel):
    authorization_url: str
    scopes: list[str]


def google_ready(settings) -> str | None:
    if settings.mode != "live":
        return "Google accounts are available in live mode only; demo mode never contacts providers"
    if not settings.google_client_id:
        return "set OPA_GOOGLE_CLIENT_ID and OPA_GOOGLE_CLIENT_SECRET to enable Google (see README)"
    return None


@router.post("/connectors/google/authorize", response_model=AuthorizeOut)
async def authorize(body: AuthorizeIn, db: DbDep, owner: OwnerDep, sess: SessionDep, settings: SettingsDep, clock: ClockDep) -> AuthorizeOut:
    reason = google_ready(settings)
    if reason:
        raise Invalid(reason, code="not_configured")
    if set(body.modes) - {"gmail", "calendar"}:
        raise Invalid("unknown capability")
    if body.connection_id:
        conn = await get_owned(db, Connection, body.connection_id, owner)
        if conn.provider != "google":
            raise Invalid("not a Google connection")
    modes = {"gmail": "off", "calendar": "off", **body.modes}
    scope_list = gscopes.scopes_for_modes(modes)
    if scope_list == gscopes.IDENTITY:
        raise Invalid("choose at least one capability to connect")
    state = secrets.token_urlsafe(32)
    verifier = generate_token(64)
    db.add(OAuthState(state_hash=sha256_hex(state), owner_id=owner.id, session_id=sess.id, provider="google", code_verifier=verifier,
                      redirect_uri=settings.google_redirect_uri, scopes=scope_list, target_connection_id=body.connection_id,
                      requested_modes=modes, expires_at=clock.now() + timedelta(minutes=settings.oauth_state_ttl_minutes)))
    client = AsyncOAuth2Client(client_id=settings.google_client_id, redirect_uri=settings.google_redirect_uri, scope=" ".join(scope_list), code_challenge_method="S256")
    url, _ = client.create_authorization_url(settings.google_auth_url, state=state, code_verifier=verifier,
                                             access_type="offline", prompt="consent", include_granted_scopes="true")
    return AuthorizeOut(authorization_url=url, scopes=scope_list)


def _back(status: str, **params: str) -> RedirectResponse:
    return RedirectResponse("/?" + urlencode({"oauth": status, **params}), status_code=303)


@router.get("/oauth/google/callback", include_in_schema=False)
async def callback(request: Request, db: DbDep, owner: OwnerDep, sess: SessionDep, settings: SettingsDep, clock: ClockDep,
                   state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    row = await db.scalar(select(OAuthState).where(OAuthState.state_hash == sha256_hex(state)).with_for_update()) if state else None
    # The state must exist, be unused, unexpired, and belong to this signed-in session.
    if row is None or row.used_at is not None or row.session_id != sess.id or row.owner_id != owner.id:
        return _back("error", reason="This sign-in link is invalid or was already used. Start again from Connections.")
    row.used_at = clock.now()
    if row.expires_at <= clock.now():
        return _back("error", reason="The Google sign-in took too long. Start again from Connections.")
    if error:
        return _back("error", reason="Google sign-in was cancelled." if error == "access_denied" else f"Google returned an error ({error}).")
    if not code:
        return _back("error", reason="Google did not return an authorization code.")
    broker = request.app.state.broker
    if broker is None:
        return _back("error", reason="The connector service is not configured.")
    candidate = new_id()
    try:
        res = await broker.google_exchange(owner_id=owner.id, candidate_connection_id=candidate, target_connection_id=row.target_connection_id,
                                           code=code, code_verifier=row.code_verifier, redirect_uri=row.redirect_uri)
    except ConnectorError as e:
        return _back("error", reason=e.message)
    row.code_verifier = "used"

    granted = res["granted_scopes"]
    conn = await db.get(Connection, res["connection_id"])
    if conn is None:
        conn = Connection(id=res["connection_id"], owner_id=owner.id, provider="google", account_label=res["email"],
                          provider_account_id=res["account_id"], modes={"gmail": "off", "calendar": "off"})
        db.add(conn)
    wanted = {**conn.modes, **{k: v for k, v in row.requested_modes.items() if v != "off" or conn.modes.get(k) is None}}
    conn.modes = {cap: gscopes.allowed_mode(cap, mode, granted) for cap, mode in wanted.items()}
    conn.granted_scopes = granted
    conn.status, conn.last_error = "connected", None
    conn.authorized_at = clock.now()
    conn.version += 1
    await db.flush()
    db.add(AuditLog(owner_id=owner.id, actor="owner", type="connection.connected", subject_id=conn.id, sanitized={"provider": "google", "scopes": granted}))
    await emit(db, owner.id, "connection.connected", {"connection_id": conn.id})
    dropped = [cap for cap, mode in wanted.items() if conn.modes[cap] != mode]
    note = ""
    if res["account_mismatch"]:
        note = f"You signed in as {res['email']}, a different account, so it was added separately."
    elif dropped:
        note = f"Google did not grant everything requested, so {', '.join(dropped)} access is limited. Reconnect to grant it."
    return _back("connected", connection=conn.id, note=note)
