"""Automations API (AUTO-01, AUTO-02, UX-01): preview, create, confirm, edit, enable, delete, history."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import select

from opa.api.deps import ClockDep, DbDep, OwnerDep, SettingsDep
from opa.db.models import Automation, Occurrence
from opa.scheduler import timing
from opa.scheduler.automations import AutomationIn, AutomationService, schedule_of

router = APIRouter(prefix="/api/automations", tags=["automations"])


class CreateIn(AutomationIn):
    confirm: bool = False  # true when the owner reviewed the preview in the UI


class VersionIn(BaseModel):
    expected_version: int


class PatchIn(BaseModel):
    expected_version: int
    name: str | None = None
    prompt: str | None = None
    time_zone: str | None = None
    local_time: str | None = None
    weekdays: list[str | int] | None = None
    run_date: str | None = None
    connection_ids: list[str] | None = None
    enabled: bool | None = None


class OccurrenceOut(BaseModel):
    scheduled_for: datetime
    local: str
    state: str
    task_id: str | None
    late: bool
    missed_count: int
    changes: int


class AutomationOut(BaseModel):
    id: str
    name: str
    kind: str
    prompt: str
    description: str
    time_zone: str
    local_time: str | None
    weekdays: list[int] | None
    run_date: str | None
    trigger: dict[str, Any] | None
    connection_ids: list[str]
    enabled: bool
    confirmed: bool
    created_by: str
    paused_reason: str | None
    next_run_at: datetime | None
    next_run_local: str | None
    last_run_at: datetime | None
    last_run_local: str | None
    completed_at: datetime | None
    version: int
    trigger_status: dict[str, Any] | None
    recent: list[OccurrenceOut]


def _describe(a: Automation) -> str:
    if a.kind == "calendar_change":
        return f"When events in the next {a.trigger['window_hours']} hours change on calendar “{a.trigger['calendar_id']}”"
    return timing.describe(schedule_of(a))


async def _out(db, a: Automation, limit: int = 5) -> AutomationOut:
    occs = list(await db.scalars(select(Occurrence).where(Occurrence.automation_id == a.id).order_by(Occurrence.scheduled_for.desc()).limit(limit)))
    st = a.trigger_state or {}
    trig = None if a.kind != "calendar_change" else {"initialized": bool(st.get("initialized")), "last_poll_at": st.get("last_poll_at"),
                                                      "last_error": st.get("last_error"), "pending_changes": len(st.get("pending", []))}
    def label(t: datetime | None) -> str | None:
        return timing.local_label(t, a.time_zone) if t else None

    return AutomationOut(
        id=a.id, name=a.name, kind=a.kind, prompt=a.prompt, description=_describe(a), time_zone=a.time_zone, local_time=a.local_time,
        weekdays=a.weekdays, run_date=a.run_date, trigger=a.trigger, connection_ids=a.connection_ids, enabled=a.enabled,
        confirmed=a.confirmed_at is not None, created_by=a.created_by, paused_reason=a.paused_reason,
        next_run_at=a.next_run_at if a.kind != "calendar_change" else None,
        next_run_local=label(a.next_run_at) if a.kind != "calendar_change" and a.enabled else None,
        last_run_at=a.last_run_at, last_run_local=label(a.last_run_at), completed_at=a.completed_at, version=a.version, trigger_status=trig,
        recent=[OccurrenceOut(scheduled_for=o.scheduled_for, local=timing.local_label(o.scheduled_for, a.time_zone), state=o.state, task_id=o.task_id,
                              late=o.late, missed_count=o.missed_count, changes=len(o.detail.get("changes", []))) for o in occs],
    )


@router.get("", response_model=list[AutomationOut])
async def list_automations(db: DbDep, owner: OwnerDep) -> list[AutomationOut]:
    rows = await db.scalars(select(Automation).where(Automation.owner_id == owner.id, Automation.deleted_at.is_(None)).order_by(Automation.created_at))
    return [await _out(db, a) for a in rows]


@router.post("/preview")
async def preview(body: AutomationIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> dict[str, Any]:
    svc = AutomationService(db, settings, clock)
    return svc.preview(await svc.normalize(owner, body))


@router.post("", response_model=AutomationOut, status_code=201)
async def create(body: CreateIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> AutomationOut:
    spec = AutomationIn(**body.model_dump(exclude={"confirm"}))
    a = await AutomationService(db, settings, clock).create(owner, spec, confirm=body.confirm, created_by="owner")
    return await _out(db, a)


@router.get("/{automation_id}", response_model=AutomationOut)
async def get_one(automation_id: str, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> AutomationOut:
    return await _out(db, await AutomationService(db, settings, clock).get(owner.id, automation_id), limit=50)


@router.post("/{automation_id}/confirm", response_model=AutomationOut)
async def confirm(automation_id: str, body: VersionIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> AutomationOut:
    """Only this authenticated owner endpoint activates an automation the assistant proposed."""
    return await _out(db, await AutomationService(db, settings, clock).confirm(owner, automation_id, body.expected_version))


@router.patch("/{automation_id}", response_model=AutomationOut)
async def patch(automation_id: str, body: PatchIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> AutomationOut:
    changes = body.model_dump(exclude={"expected_version"}, exclude_none=True)
    return await _out(db, await AutomationService(db, settings, clock).update(owner, automation_id, body.expected_version, changes))


@router.delete("/{automation_id}", status_code=204)
async def delete(automation_id: str, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> Response:
    await AutomationService(db, settings, clock).delete(owner, automation_id)
    return Response(status_code=204)
