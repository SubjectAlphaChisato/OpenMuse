"""Account changes asked for in chat (APP-01, CON-01).

The assistant may ask to connect an account, change what it may do with one, or disconnect it. The
request becomes a card in the conversation. Anything that adds access, and every disconnect, happens
only when the owner confirms the card through the authenticated API; the assistant cannot confirm its
own request. A change that only reduces access is applied at once, because it can never widen what the
assistant may do, and the card records it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.api.tasks_service import add_message
from opa.connectors import registry
from opa.core.errors import Conflict, Invalid, NotFound
from opa.db.models import Connection, ConnectionRequest, Conversation
from opa.db.outbox import emit

RANK = {"off": 0, "read_only": 1, "read_write": 2}
MODE_LABEL = {"off": "Off", "read_only": "Read only", "read_write": "Read and propose changes"}
TTL = timedelta(hours=24)
OPEN = ("pending", "waiting_for_provider")


class ChangeOut(BaseModel):
    capability: str
    label: str
    before: str
    after: str


class ConnectionRequestOut(BaseModel):
    id: str
    action: str
    provider: str
    provider_label: str
    connection_id: str | None
    account_label: str | None
    title: str
    changes: list[ChangeOut]
    note: str
    reason: str
    status: str
    result: str | None
    confirm_label: str
    expires_at: datetime
    created_at: datetime


def _cap_label(provider: str, cap: str) -> str:
    spec = registry.get(provider)
    c = spec.capability(cap) if spec else None
    return c.label if c else cap


def describe(req: ConnectionRequest, conn: Connection | None, now: datetime) -> ConnectionRequestOut:
    """What the card shows. Built only from stored, validated fields, never from model text (except `reason`, shown as the assistant's words)."""
    name = registry.SHORT.get(req.provider, req.provider)
    label = conn.account_label if conn else None
    spec = registry.get(req.provider)
    order = spec.capability_ids if spec else []
    caps = sorted(req.modes, key=lambda c: order.index(c) if c in order else len(order))  # JSONB does not keep key order
    changes = [ChangeOut(capability=c, label=_cap_label(req.provider, c), before=req.modes[c]["before"], after=req.modes[c]["after"]) for c in caps]
    if req.action == "connect":
        title = "Connect the demo account" if req.provider == "fixture" else f"Connect {name}"
        note = ("Sample mail, calendar, and documents; no real provider is contacted." if req.provider == "fixture"
                else f"You will sign in to {name} and choose what to share. The assistant never sees your password.")
        confirm = "Connect" if req.provider == "fixture" else f"Continue to {name}"
    elif req.action == "reconnect":
        title = f"Reconnect {label or name}"
        note = f"{name} stopped accepting the saved sign-in. You will sign in again; access stays as it was."
        confirm = f"Continue to {name}"
    elif req.action == "change":
        title = f"Change access for {label or name}"
        widening = any(RANK[c.after] > RANK[c.before] for c in changes)
        note = ("Changes still need your approval each time, even with this access." if widening
                else "This only reduces access, so it was applied right away.")
        confirm = "Allow"
    else:
        title = f"Disconnect {label or name}"
        note = "The assistant loses access to this account. Waiting approvals for it are cancelled, and automations that use it pause."
        confirm = "Disconnect"
    status = "expired" if req.status in OPEN and req.expires_at <= now else req.status
    return ConnectionRequestOut(id=req.id, action=req.action, provider=req.provider, provider_label=name, connection_id=req.connection_id,
                                account_label=label, title=title, changes=changes, note=note, reason=req.reason, status=status, result=req.result,
                                confirm_label=confirm, expires_at=req.expires_at, created_at=req.created_at)


async def describe_many(db: AsyncSession, ids: list[str], now: datetime) -> dict[str, ConnectionRequestOut]:
    if not ids:
        return {}
    reqs = list(await db.scalars(select(ConnectionRequest).where(ConnectionRequest.id.in_(ids))))
    conn_ids = [r.connection_id for r in reqs if r.connection_id]
    conns = {c.id: c for c in await db.scalars(select(Connection).where(Connection.id.in_(conn_ids)))} if conn_ids else {}
    return {r.id: describe(r, conns.get(r.connection_id or ""), now) for r in reqs}


def _summary(action: str, provider: str, account: str | None, changes: dict[str, dict[str, str]]) -> str:
    name = registry.SHORT.get(provider, provider)
    parts = ", ".join(f"{_cap_label(provider, c)}: {MODE_LABEL[m['after']]}" for c, m in changes.items())  # in the order given
    if action == "connect":
        return f"Request: connect {name}" + (f" ({parts})" if parts else "") + "."
    if action == "change":
        return f"Request: change access for {account or name} ({parts})."
    if action == "reconnect":
        return f"Request: reconnect {account or name}."
    return f"Request: disconnect {account or name}."


async def _resolve_connection(db: AsyncSession, owner_id: str, provider: str | None, connection_id: str | None) -> Connection:
    if connection_id:
        conn = await db.get(Connection, connection_id)
        if conn is None or conn.owner_id != owner_id or conn.status == "disconnected":
            raise Invalid("no such connected account; call connections__list for connection IDs", code="no_connection")
        if provider and conn.provider != provider:
            raise Invalid(f"that account is a {registry.SHORT.get(conn.provider, conn.provider)} account, not {provider}")
        return conn
    if not provider:
        raise Invalid("give a provider or a connection_id")
    conns = list(await db.scalars(select(Connection).where(Connection.owner_id == owner_id, Connection.provider == provider,
                                                           Connection.status != "disconnected")))
    if not conns:
        raise Invalid(f"no {registry.SHORT.get(provider, provider)} account is connected; ask to connect one instead", code="no_connection")
    if len(conns) > 1:
        raise Invalid("more than one account matches; pass connection_id: " + "; ".join(f"{c.id} = {c.account_label}" for c in conns), code="ambiguous")
    return conns[0]


async def create(db: AsyncSession, settings: Any, clock: Any, *, owner_id: str, conversation_id: str, task_id: str | None, action: str,
                 provider: str | None, connection_id: str | None, modes: dict[str, str] | None, reason: str) -> ConnectionRequest:
    """Validate and record a request from the assistant, and post its card in the conversation."""
    from opa.api.routers.connections import apply_modes, check_modes
    from opa.api.routers.oauth import DEFAULT_MODES

    now = clock.now()
    conn: Connection | None = None
    changes: dict[str, dict[str, str]] = {}
    if action == "connect":
        spec = registry.get(provider or "")
        if spec is None or (not spec.oauth and settings.mode != "demo"):
            raise Invalid(f"unknown provider {provider}; choose one from connections__list", code="unknown_provider")
        why = registry.ready(settings, spec.id)
        if why:
            raise Invalid(why, code="not_configured")
        if not spec.oauth and await db.scalar(select(Connection.id).where(Connection.owner_id == owner_id, Connection.provider == spec.id,
                                                                            Connection.status != "disconnected")):
            raise Invalid("the demo account is already connected; ask to change its access instead", code="already_connected")
        wanted = modes if modes else DEFAULT_MODES.get(spec.id, {c: "read_only" for c in spec.capability_ids})
        check_modes(spec.id, wanted)
        if all(m == "off" for m in wanted.values()):
            raise Invalid("choose at least one capability to connect")
        changes = {c: {"before": "off", "after": m} for c, m in wanted.items() if m != "off"}
        provider = spec.id
    elif action in ("change", "disconnect", "reconnect"):
        conn = await _resolve_connection(db, owner_id, provider, connection_id)
        provider = conn.provider
        if action == "change":
            if not modes:
                raise Invalid("modes is required to change access")
            check_modes(provider, modes)
            current = registry.full_modes(provider, conn.modes)
            changes = {c: {"before": current.get(c, "off"), "after": m} for c, m in modes.items() if current.get(c, "off") != m}
            if not changes:
                raise Invalid("access is already set that way", code="no_change")
    else:
        raise Invalid("action must be connect, change, reconnect, or disconnect")

    # An older open request for the same thing in this conversation is replaced by this one. Access changes
    # replace each other only when they touch the same capability.
    older = await db.scalars(select(ConnectionRequest).where(
        ConnectionRequest.owner_id == owner_id, ConnectionRequest.conversation_id == conversation_id, ConnectionRequest.action == action,
        ConnectionRequest.provider == provider, ConnectionRequest.connection_id.is_(None) if conn is None else ConnectionRequest.connection_id == conn.id,
        ConnectionRequest.status.in_(OPEN)).with_for_update())
    for old in older:
        if action != "change" or set(old.modes) & set(changes):
            old.status, old.decided_at = "superseded", now
    req = ConnectionRequest(owner_id=owner_id, conversation_id=conversation_id, task_id=task_id, action=action, provider=provider,
                            connection_id=conn.id if conn else None, modes=changes, reason=reason.strip()[:300], status="pending",
                            expires_at=now + TTL)
    db.add(req)
    await db.flush()
    if action == "change" and all(RANK[m["after"]] <= RANK[m["before"]] for m in changes.values()):
        await apply_modes(db, conn, {c: m["after"] for c, m in changes.items()}, owner_id, clock)
        req.status, req.result, req.decided_at = "completed", "Applied right away because it only reduces access.", now
    conv = await db.get(Conversation, conversation_id)
    msg = await add_message(db, conv, "assistant", _summary(action, provider, conn.account_label if conn else None, changes), task_id=task_id)
    msg.connection_request_id = req.id
    await emit(db, owner_id, "connection_request.created", {"request_id": req.id, "action": action, "status": req.status}, task_id=task_id)
    return req


async def _load_open(db: AsyncSession, request_id: str, owner_id: str, now: datetime) -> ConnectionRequest:
    req = await db.scalar(select(ConnectionRequest).where(ConnectionRequest.id == request_id).with_for_update())
    if req is None or req.owner_id != owner_id:
        raise NotFound("request not found")
    if req.status not in OPEN:
        raise Conflict(f"this request is already {req.status}", code="invalid_state")
    if req.expires_at <= now:
        req.status, req.decided_at = "expired", now
        raise Conflict("this request expired; ask the assistant again", code="expired")
    return req


async def decline(db: AsyncSession, request_id: str, owner_id: str, clock: Any) -> ConnectionRequest:
    req = await _load_open(db, request_id, owner_id, clock.now())
    req.status, req.decided_at, req.result = "declined", clock.now(), "You declined this request."
    await emit(db, owner_id, "connection_request.updated", {"request_id": req.id, "status": req.status})
    return req


async def confirm(db: AsyncSession, request_id: str, owner: Any, session_id: str, settings: Any, clock: Any, http_request: Any) -> tuple[ConnectionRequest, str | None]:
    """The owner confirmed the card. Returns the request and, when the provider must be asked, the sign-in URL."""
    from opa.api.routers.connections import apply_modes, connect_demo, disconnect
    from opa.api.routers.oauth import start_authorization

    now = clock.now()
    req = await _load_open(db, request_id, owner.id, now)
    target = {c: m["after"] for c, m in req.modes.items()}
    url: str | None = None
    if req.action == "connect":
        if req.provider == "fixture":
            conn = await connect_demo(db, owner.id, settings, target)
            req.connection_id = conn.id
            req.status, req.result = "completed", f"Connected {conn.account_label}."
        else:
            url = (await start_authorization(db, owner, session_id, settings, clock, req.provider, target, None, req.id)).authorization_url
            req.status = "waiting_for_provider"
    elif req.action == "reconnect":
        conn = await db.get(Connection, req.connection_id)
        if conn is None or conn.status == "disconnected":
            raise Conflict("that account is no longer connected", code="invalid_state")
        if conn.provider == "fixture":
            raise Conflict("the demo account does not need reconnecting", code="invalid_state")
        url = (await start_authorization(db, owner, session_id, settings, clock, conn.provider,
                                         registry.full_modes(conn.provider, conn.modes), conn.id, req.id)).authorization_url
        req.status = "waiting_for_provider"
    elif req.action == "change":
        conn = await db.get(Connection, req.connection_id)
        if conn is None or conn.status == "disconnected":
            raise Conflict("that account is no longer connected", code="invalid_state")
        try:
            await apply_modes(db, conn, target, owner.id, clock)
            req.status, req.result = "completed", "Access updated."
        except Conflict as e:
            if e.code != "scope_required":
                raise
            # The provider must grant more first; the change completes when the sign-in returns.
            url = (await start_authorization(db, owner, session_id, settings, clock, conn.provider,
                                             {**registry.full_modes(conn.provider, conn.modes), **target}, conn.id, req.id)).authorization_url
            req.status = "waiting_for_provider"
    else:
        res = await disconnect(req.connection_id, http_request, owner, clock)  # commits the denial first, then revokes
        req.status = "completed"
        req.result = "Disconnected." if res.revoked else f"Disconnected here, but the provider did not confirm revocation: {res.revocation_error}"
    if req.status == "completed":
        req.decided_at = now
    await emit(db, owner.id, "connection_request.updated", {"request_id": req.id, "status": req.status})
    return req, url


async def finish_from_callback(db: AsyncSession, request_id: str, conn: Connection, note: str, clock: Any) -> None:
    """A sign-in started from a chat card came back successfully."""
    req = await db.get(ConnectionRequest, request_id)
    if req is None or req.owner_id != conn.owner_id or req.status not in OPEN:
        return
    req.connection_id = conn.id
    req.status, req.decided_at = "completed", clock.now()
    req.result = f"Connected {conn.account_label}." + (f" {note}" if note else "")
    await emit(db, conn.owner_id, "connection_request.updated", {"request_id": req.id, "status": req.status})


def model_view(db_conns: list[Connection], settings: Any) -> dict[str, Any]:
    """What connections__list returns to the model: accounts, their access, and what can be connected."""
    accounts = [{"connection_id": c.id, "provider": c.provider, "account": c.account_label, "status": c.status,
                 "access": registry.full_modes(c.provider, c.modes)} for c in db_conns]
    providers = []
    for pid, spec in registry.PROVIDERS.items():
        if not spec.oauth and settings.mode != "demo":
            continue
        why = registry.ready(settings, pid)
        providers.append({"provider": pid, "label": spec.label, "can_connect": why is None, "reason": why,
                          "capabilities": registry.describe(pid)})
    return {"accounts": accounts, "providers": providers}
