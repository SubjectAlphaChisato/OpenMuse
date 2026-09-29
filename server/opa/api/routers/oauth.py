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
from opa.connectors import registry
from opa.core.canonical import sha256_hex
from opa.core.errors import Invalid
from opa.core.ids import new_id
from opa.db.models import AuditLog, Connection, OAuthState
from opa.db.outbox import emit
from opa.sdk.contract import ConnectorError

router = APIRouter(prefix="/api", tags=["oauth"])
Mode = Literal["off", "read_only", "read_write"]


class AuthorizeIn(BaseModel):
    connection_id: str | None = None  # set for reconnect or to add access to an existing connection
    modes: dict[str, Mode] | None = None  # default: the provider's usual read-only set


class AuthorizeOut(BaseModel):
    authorization_url: str
    scopes: list[str]


DEFAULT_MODES = {"google": {"gmail": "read_only", "calendar": "read_only"}}  # Drive is opt-in: its scope is restricted


def google_ready(settings) -> str | None:  # kept for callers from M3
    return registry.ready(settings, "google")


def _authorization_url(settings, provider: str, redirect: str, scope_list: list[str], state: str, verifier: str) -> str:
    """Built with Authlib for every provider; parameters follow docs/providers/*.md."""
    spec = registry.PROVIDERS[provider]
    kw = {"code_challenge_method": "S256"} if spec.pkce else {}
    client = AsyncOAuth2Client(client_id=registry.client_id(settings, provider), redirect_uri=redirect,
                               scope=" ".join(scope_list) or None, **kw)
    if provider == "google":
        extra = {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}
        url_base = settings.google_auth_url
    elif provider == "notion":
        extra, url_base = {"owner": "user"}, settings.notion_auth_url
    else:
        extra, url_base = {}, settings.github_auth_url
    url, _ = client.create_authorization_url(url_base, state=state, code_verifier=verifier if spec.pkce else None, **extra)
    return url


@router.post("/connectors/{provider}/authorize", response_model=AuthorizeOut)
async def authorize(provider: str, body: AuthorizeIn, db: DbDep, owner: OwnerDep, sess: SessionDep, settings: SettingsDep, clock: ClockDep) -> AuthorizeOut:
    spec = registry.get(provider)
    if spec is None or not spec.oauth:
        raise Invalid(f"{provider} is not a provider you sign in to", code="unknown_provider")
    reason = registry.ready(settings, provider)
    if reason:
        raise Invalid(reason, code="not_configured")
    requested = body.modes if body.modes is not None else DEFAULT_MODES.get(provider, {c: "read_only" for c in spec.capability_ids})
    unknown = set(requested) - set(spec.capability_ids)
    if unknown:
        raise Invalid(f"unknown capability for {provider}: {sorted(unknown)}")
    if body.connection_id:
        conn = await get_owned(db, Connection, body.connection_id, owner)
        if conn.provider != provider:
            raise Invalid(f"not a {provider} connection")
    modes = {**{c: "off" for c in spec.capability_ids}, **requested}
    if all(m == "off" for m in modes.values()):
        raise Invalid("choose at least one capability to connect")
    scope_list = registry.scopes_for_modes(provider, modes)
    state = secrets.token_urlsafe(32)
    verifier = generate_token(64)
    redirect = registry.redirect_uri(settings, provider)
    db.add(OAuthState(state_hash=sha256_hex(state), owner_id=owner.id, session_id=sess.id, provider=provider, code_verifier=verifier,
                      redirect_uri=redirect, scopes=scope_list, target_connection_id=body.connection_id,
                      requested_modes=modes, expires_at=clock.now() + timedelta(minutes=settings.oauth_state_ttl_minutes)))
    return AuthorizeOut(authorization_url=_authorization_url(settings, provider, redirect, scope_list, state, verifier), scopes=scope_list)


def _back(status: str, **params: str) -> RedirectResponse:
    return RedirectResponse("/?" + urlencode({"oauth": status, **params}), status_code=303)


@router.get("/oauth/{provider}/callback", include_in_schema=False)
async def callback(provider: str, request: Request, db: DbDep, owner: OwnerDep, sess: SessionDep, settings: SettingsDep, clock: ClockDep,
                   state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    name = registry.SHORT.get(provider, provider)
    row = await db.scalar(select(OAuthState).where(OAuthState.state_hash == sha256_hex(state)).with_for_update()) if state else None
    # The state must exist, be unused, unexpired, belong to this signed-in session, and be for this provider.
    if row is None or row.used_at is not None or row.session_id != sess.id or row.owner_id != owner.id or row.provider != provider:
        return _back("error", reason="This sign-in link is invalid or was already used. Start again from Connections.")
    row.used_at = clock.now()
    if row.expires_at <= clock.now():
        return _back("error", reason=f"The {name} sign-in took too long. Start again from Connections.")
    if error:
        return _back("error", reason=f"{name} sign-in was cancelled." if error == "access_denied" else f"{name} returned an error ({error}).")
    if not code:
        return _back("error", reason=f"{name} did not return an authorization code.")
    broker = request.app.state.broker
    if broker is None:
        return _back("error", reason="The connector service is not configured.")
    candidate = new_id()
    try:
        res = await broker.oauth_exchange(provider, owner_id=owner.id, candidate_connection_id=candidate, target_connection_id=row.target_connection_id,
                                          code=code, code_verifier=row.code_verifier, redirect_uri=row.redirect_uri)
    except ConnectorError as e:
        return _back("error", reason=e.message)
    row.code_verifier = "used"

    granted = res["granted_scopes"]
    conn = await db.get(Connection, res["connection_id"])
    if conn is None:
        conn = Connection(id=res["connection_id"], owner_id=owner.id, provider=provider, account_label=res["label"],
                          provider_account_id=res["account_id"], modes=registry.full_modes(provider, {}), version=0)
        db.add(conn)
    current = registry.full_modes(provider, conn.modes)
    wanted = {**current, **{k: v for k, v in row.requested_modes.items() if v != "off" or k not in conn.modes}}
    conn.modes = {cap: registry.allowed_mode(provider, cap, mode, granted) for cap, mode in wanted.items()}
    conn.granted_scopes = granted
    conn.status, conn.last_error = "connected", None
    conn.authorized_at = clock.now()
    conn.version += 1
    await db.flush()
    db.add(AuditLog(owner_id=owner.id, actor="owner", type="connection.connected", subject_id=conn.id, sanitized={"provider": provider, "scopes": granted}))
    await emit(db, owner.id, "connection.connected", {"connection_id": conn.id})
    dropped = [cap for cap, mode in wanted.items() if conn.modes[cap] != mode]
    note = ""
    if res["account_mismatch"]:
        note = f"You signed in as {res['label']}, a different account, so it was added separately."
    elif dropped:
        note = f"{name} did not grant everything requested, so {', '.join(dropped)} access is limited. Reconnect to grant it."
    return _back("connected", connection=conn.id, note=note)
