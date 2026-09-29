"""v0.2 harness API: the owner's skills, and the account requests the assistant made in chat."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select

from opa.api.deps import ClockDep, DbDep, OwnerDep, SessionDep, SettingsDep
from opa.connectors import requests as conn_requests
from opa.connectors.requests import ConnectionRequestOut
from opa.core.errors import NotFound
from opa.db.models import Connection
from opa.db.outbox import emit
from opa.skills import harness

router = APIRouter(prefix="/api", tags=["skills"])


class SkillOut(BaseModel):
    name: str
    title: str
    description: str
    summary: str  # for the owner; description is what the model sees
    status: str
    detail: str
    requires: str | None
    builtin: bool
    enabled: bool
    tools: list[str]
    skills: list[str]


class SkillPatch(BaseModel):
    enabled: bool


class ConfirmOut(BaseModel):
    request: ConnectionRequestOut
    authorization_url: str | None = None  # set when the owner must sign in to the provider next


async def _catalog(request: Request, db, owner, settings) -> list[SkillOut]:
    lib = harness.library_for(settings.skills_dir)
    conns = list(await db.scalars(select(Connection).where(Connection.owner_id == owner.id, Connection.status != "disconnected")))
    browser_ready = getattr(request.app.state, "browser", None) is not None
    return [SkillOut(**harness.as_dict(s)) for s in harness.statuses(lib, settings, conns, harness.disabled_skills(owner), browser_ready)]


@router.get("/skills", response_model=list[SkillOut])
async def list_skills(request: Request, db: DbDep, owner: OwnerDep, settings: SettingsDep) -> list[SkillOut]:
    return await _catalog(request, db, owner, settings)


@router.patch("/skills/{name}", response_model=SkillOut)
async def set_skill(name: str, body: SkillPatch, request: Request, db: DbDep, owner: OwnerDep, settings: SettingsDep) -> SkillOut:
    """Turning a skill off hides it from the assistant and refuses its tools. It changes no account access."""
    if harness.library_for(settings.skills_dir).get(name) is None:
        raise NotFound("no such skill")
    disabled = harness.disabled_skills(owner)
    disabled = disabled - {name} if body.enabled else disabled | {name}
    owner.preferences = {**(owner.preferences or {}), "disabled_skills": sorted(disabled)}
    await emit(db, owner.id, "skill.updated", {"skill": name, "enabled": body.enabled})
    return next(s for s in await _catalog(request, db, owner, settings) if s.name == name)


@router.post("/connection-requests/{request_id}/confirm", response_model=ConfirmOut)
async def confirm_request(request_id: str, request: Request, db: DbDep, owner: OwnerDep, sess: SessionDep, settings: SettingsDep,
                          clock: ClockDep) -> ConfirmOut:
    """Only this authenticated owner action carries out an account change the assistant asked for."""
    req, url = await conn_requests.confirm(db, request_id, owner, sess.id, settings, clock, request)
    await db.flush()
    conn = await db.get(Connection, req.connection_id) if req.connection_id else None
    return ConfirmOut(request=conn_requests.describe(req, conn, clock.now()), authorization_url=url)


@router.post("/connection-requests/{request_id}/decline", response_model=ConnectionRequestOut)
async def decline_request(request_id: str, db: DbDep, owner: OwnerDep, clock: ClockDep) -> ConnectionRequestOut:
    req = await conn_requests.decline(db, request_id, owner.id, clock)
    conn = await db.get(Connection, req.connection_id) if req.connection_id else None
    return conn_requests.describe(req, conn, clock.now())
