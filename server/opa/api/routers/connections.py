from __future__ import annotations

from typing import Literal

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select

from opa.api.actions_service import invalidate_actions, open_actions_for_connection
from opa.api.deps import ClockDep, DbDep, OwnerDep, SettingsDep, get_owned
from opa.api.routers.oauth import google_ready
from opa.broker.google import scopes as gscopes
from opa.scheduler.automations import pause_for_connection
from opa.sdk.contract import ConnectorError
from opa.core.errors import Conflict, Invalid
from opa.db.models import Connection
from opa.db.outbox import emit

router = APIRouter(prefix="/api", tags=["connections"])

Mode = Literal["off", "read_only", "read_write"]
CAPABILITIES = ("gmail", "calendar")


class ConnectorOut(BaseModel):
    provider: str
    label: str
    capabilities: list[str]
    available: bool
    reason: str | None = None


class ConnectionOut(BaseModel):
    id: str
    provider: str
    account_label: str
    status: str
    modes: dict[str, str]
    granted_scopes: list[str]
    version: int
    is_demo: bool
    last_error: str | None = None
    last_health_at: datetime | None = None
    authorized_at: datetime | None = None
    warning: str | None = None


class ConnectIn(BaseModel):
    provider: Literal["fixture", "google"]


class ModesIn(BaseModel):
    modes: dict[str, Mode]
    expected_version: int


def out(c: Connection, settings=None, now: datetime | None = None) -> ConnectionOut:
    warning = None
    if c.provider == "google" and settings is not None and settings.google_oauth_testing and c.authorized_at and now and c.status == "connected":
        if now >= c.authorized_at + timedelta(days=6):
            warning = "This Google app is in Testing mode, so Google ends access 7 days after sign-in. Reconnect soon to avoid interruption."
    return ConnectionOut(id=c.id, provider=c.provider, account_label=c.account_label, status=c.status, modes=c.modes,
                         granted_scopes=c.granted_scopes, version=c.version, is_demo=c.provider == "fixture", last_error=c.last_error,
                         last_health_at=c.last_health_at, authorized_at=c.authorized_at, warning=warning)


@router.get("/connectors", response_model=list[ConnectorOut])
async def connectors(owner: OwnerDep, settings: SettingsDep) -> list[ConnectorOut]:
    reason = google_ready(settings)
    items = [ConnectorOut(provider="google", label="Google (Gmail and Calendar)", capabilities=list(CAPABILITIES), available=reason is None, reason=reason)]
    if settings.mode == "demo":
        items.append(ConnectorOut(provider="fixture", label="Demo account (sample mail and calendar, no real provider)", capabilities=list(CAPABILITIES), available=True))
    return items


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> list[ConnectionOut]:
    rows = await db.scalars(select(Connection).where(Connection.owner_id == owner.id, Connection.status != "disconnected").order_by(Connection.created_at))
    return [out(c, settings, clock.now()) for c in rows]


@router.post("/connections", response_model=ConnectionOut, status_code=201)
async def connect(body: ConnectIn, db: DbDep, owner: OwnerDep, settings: SettingsDep) -> ConnectionOut:
    if body.provider == "google":
        raise Invalid("connect Google through POST /api/connectors/google/authorize", code="use_authorize")
    if settings.mode != "demo":
        raise Invalid("demo accounts are only available in demo mode", code="not_configured")
    conn = Connection(owner_id=owner.id, provider="fixture", account_label="demo@example.test", status="connected",
                      modes={c: "read_only" for c in CAPABILITIES})  # APP-01: read-only by default
    db.add(conn)
    await db.flush()
    await emit(db, owner.id, "connection.connected", {"connection_id": conn.id})
    return out(conn)


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
async def change_modes(connection_id: str, body: ModesIn, db: DbDep, owner: OwnerDep, clock: ClockDep) -> ConnectionOut:
    conn = await get_owned(db, Connection, connection_id, owner)
    if conn.status == "disconnected":
        raise Conflict("connection is disconnected", code="invalid_state")
    if conn.version != body.expected_version:
        raise Conflict("connection changed; reload and try again", code="version_conflict")
    unknown = set(body.modes) - set(CAPABILITIES)
    if unknown:
        raise Invalid(f"unknown capabilities: {sorted(unknown)}")
    if conn.provider == "google":
        missing = sorted({s for cap, mode in body.modes.items() for s in gscopes.missing_for_mode(cap, mode, conn.granted_scopes)})
        if missing:
            # Tool permission is the owner's choice; provider scopes are Google's grant. Ask Google first.
            raise Conflict("Google has not granted this yet; continue to Google to grant: " + ", ".join(missing), code="scope_required")
    new = {**conn.modes, **body.modes}
    for cap, mode in body.modes.items():
        if mode != "read_write":
            await invalidate_actions(db, await open_actions_for_connection(db, conn.id, cap, "write"), f"{cap} access was reduced to {mode}", clock, block_code="permission_changed")
        if mode == "off":
            await invalidate_actions(db, await open_actions_for_connection(db, conn.id, cap), f"{cap} access was turned off", clock, block_code="permission_changed")
    conn.modes = new
    conn.version += 1
    await emit(db, owner.id, "connection.updated", {"connection_id": conn.id, "modes": new})
    return out(conn)


class DisconnectOut(ConnectionOut):
    revoked: bool
    revocation_error: str | None = None


@router.delete("/connections/{connection_id}", response_model=DisconnectOut)
async def disconnect(connection_id: str, request: Request, owner: OwnerDep, clock: ClockDep) -> DisconnectOut:
    """CON-01: deny new calls and invalidate approvals first (committed), then clear credentials and
    attempt provider revocation. A failed revocation is reported; local access is not restored."""
    sm = request.app.state.sessionmaker
    async with sm() as db, db.begin():
        conn = await get_owned(db, Connection, connection_id, owner)
        conn.status = "disconnected"
        conn.version += 1
        await invalidate_actions(db, await open_actions_for_connection(db, conn.id), "the account was disconnected", clock, block_code="connection_disconnected")
        await pause_for_connection(db, owner.id, conn.id, "connection_disconnected")
    revoked, err = conn.provider == "fixture", None
    broker = request.app.state.broker
    if conn.provider != "fixture":
        if broker is None:
            err = "the connector service is not configured; credentials were not cleared"
        else:
            try:
                res = await broker.revoke(conn.id, owner.id)
                revoked, err = res["revoked"], res["error"]
            except Exception as e:  # noqa: BLE001 - report, never restore access
                err = f"revocation could not be attempted: {e.__class__.__name__}"
    async with sm() as db, db.begin():
        conn = await db.get(Connection, connection_id)
        conn.last_error = None if revoked else err
        body = conn.account_label if revoked else f"{conn.account_label}. Access was removed here, but Google revocation failed: {err}"
        await emit(db, owner.id, "connection.disconnected", {"connection_id": conn.id, "revoked": revoked}, notify=("Account disconnected", body))
    return DisconnectOut(**out(conn).model_dump(), revoked=revoked, revocation_error=err)


class HealthOut(BaseModel):
    status: str
    detail: str | None = None


@router.post("/connections/{connection_id}/health", response_model=HealthOut)
async def health(connection_id: str, request: Request, db: DbDep, owner: OwnerDep) -> HealthOut:
    conn = await get_owned(db, Connection, connection_id, owner)
    broker = request.app.state.broker
    if broker is None:
        raise Invalid("the connector service is not configured", code="not_configured")
    try:
        res = await broker.health(conn.id, owner.id)
    except ConnectorError as e:
        raise Invalid(e.message, code=e.code) from None
    return HealthOut(**res)
