"""Automation lifecycle shared by the API and the agent's tools (AUTO-01, AUTO-02).

Every schedule is normalized and previewed before it can run. Automations the assistant proposes
stay inactive until the owner confirms them through the authenticated API.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import any_, select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.api.actions_service import invalidate_actions, open_actions_for_task
from opa.api.tasks_service import transition
from opa.core.clock import Clock
from opa.core.errors import Conflict, Invalid, NotFound
from opa.db.models import TERMINAL_STATES, Automation, Connection, Conversation, Occurrence, Owner, Task
from opa.db.outbox import emit
from opa.scheduler import timing
from opa.scheduler.timing import Schedule, ScheduleError

SCHEDULE_KINDS = ("once", "daily", "weekly")


class CalendarTriggerIn(BaseModel):
    connection_id: str
    calendar_id: str = "primary"


class AutomationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["once", "daily", "weekly", "calendar_change"]
    prompt: str = Field(min_length=1, max_length=4000)
    time_zone: str | None = None
    local_time: str | None = None
    weekdays: list[str | int] | None = None
    run_date: str | None = None
    connection_ids: list[str] = Field(default_factory=list)
    calendar: CalendarTriggerIn | None = None
    conversation_id: str | None = None


@dataclass
class Normalized:
    values: dict[str, Any]
    description: str
    upcoming: list[datetime]


def schedule_of(a: Automation | dict[str, Any]) -> Schedule:
    get = (lambda k: a[k]) if isinstance(a, dict) else (lambda k: getattr(a, k))
    return Schedule(get("kind"), get("time_zone"), get("local_time"), tuple(get("weekdays") or ()), get("run_date"))


class AutomationService:
    def __init__(self, db: AsyncSession, settings, clock: Clock) -> None:
        self.db, self.settings, self.clock = db, settings, clock

    # ------------------------------------------------------------ normalization
    async def normalize(self, owner: Owner, spec: AutomationIn) -> Normalized:
        now = self.clock.now()
        tz = spec.time_zone or owner.time_zone or "UTC"
        try:
            timing.zone_or_error(tz)
            conn_ids = list(dict.fromkeys(spec.connection_ids))
            values: dict[str, Any] = {"name": spec.name.strip(), "kind": spec.kind, "prompt": spec.prompt.strip(), "time_zone": tz,
                                      "local_time": None, "weekdays": None, "run_date": None, "trigger": None}
            if spec.kind in SCHEDULE_KINDS:
                if not spec.local_time:
                    raise ScheduleError("local_time is required, e.g. 18:00")
                values["local_time"] = timing.parse_local_time(spec.local_time)
                if spec.kind == "weekly":
                    values["weekdays"] = list(timing.parse_weekdays(spec.weekdays or []))
                if spec.kind == "once":
                    try:
                        values["run_date"] = date.fromisoformat(spec.run_date or "").isoformat()
                    except ValueError:
                        raise ScheduleError("run_date must be YYYY-MM-DD") from None
                sched = schedule_of(values)
                upcoming = timing.upcoming(sched, now, 3)
                if spec.kind == "once" and not upcoming:
                    at = timing.resolve(date.fromisoformat(values["run_date"]), sched.wall, sched.zone)
                    nxt = timing.resolve(now.astimezone(sched.zone).date() + timedelta(days=1), sched.wall, sched.zone)
                    raise ScheduleError(f"that time has already passed ({timing.local_label(at, tz)}). "
                                        f"Did you mean {timing.local_label(nxt, tz)}?")
                description = timing.describe(sched)
            else:
                if spec.calendar is None:
                    raise ScheduleError("a calendar-change trigger needs calendar.connection_id and calendar.calendar_id")
                conn = await self._usable_connection(owner, spec.calendar.connection_id)
                if conn.modes.get("calendar", "off") == "off":
                    raise ScheduleError(f"Calendar access is off for {conn.account_label}")
                if conn.id not in conn_ids:
                    conn_ids.insert(0, conn.id)
                hours = self.settings.trigger_window_hours
                values["trigger"] = {"connection_id": conn.id, "calendar_id": spec.calendar.calendar_id, "window_hours": hours,
                                     "poll_seconds": self.settings.trigger_poll_seconds, "debounce_seconds": self.settings.trigger_debounce_seconds}
                description = (f"When events in the next {hours} hours change on calendar “{spec.calendar.calendar_id}” of "
                               f"{conn.account_label} (checked every {self.settings.trigger_poll_seconds // 60} minutes)")
                upcoming = []
            for cid in conn_ids:
                await self._usable_connection(owner, cid)
        except ScheduleError as e:
            raise Invalid(str(e), code="invalid_schedule") from None
        values["connection_ids"] = conn_ids
        values["conversation_id"] = await self._conversation_id(owner, spec.conversation_id)
        return Normalized(values, description, upcoming)

    async def _usable_connection(self, owner: Owner, connection_id: str) -> Connection:
        conn = await self.db.get(Connection, connection_id)
        if conn is None or conn.owner_id != owner.id or conn.status == "disconnected":
            raise ScheduleError(f"connection {connection_id} is not one of your connected accounts")
        return conn

    async def _conversation_id(self, owner: Owner, conversation_id: str | None) -> str:
        if conversation_id:
            conv = await self.db.get(Conversation, conversation_id)
            if conv is None or conv.owner_id != owner.id or conv.deleted_at is not None:
                raise Invalid("conversation not found")
            return conv.id
        from opa.api.routers.conversations import ensure_default

        return (await ensure_default(self.db, owner)).id

    def preview(self, n: Normalized) -> dict[str, Any]:
        tz = n.values["time_zone"]
        return {"normalized": n.values, "description": n.description,
                "upcoming": [{"at": at.isoformat(), "local": timing.local_label(at, tz)} for at in n.upcoming]}

    # ------------------------------------------------------------ lifecycle
    def _first_run(self, a: Automation) -> datetime | None:
        if a.kind == "calendar_change":
            return self.clock.now()  # first poll establishes the baseline and notifies nothing
        return timing.next_after(schedule_of(a), self.clock.now())

    async def create(self, owner: Owner, spec: AutomationIn, *, confirm: bool, created_by: str, source_task_id: str | None = None) -> Automation:
        n = await self.normalize(owner, spec)
        a = Automation(owner_id=owner.id, created_by=created_by, source_task_id=source_task_id, enabled=confirm, trigger_state={}, **n.values)
        if confirm:
            a.confirmed_at = self.clock.now()
            a.next_run_at = self._first_run(a)
        self.db.add(a)
        await self.db.flush()
        notify = None if confirm else ("Confirm a new automation", f"{a.name}: {n.description}. It will not run until you confirm it in Automations.")
        await emit(self.db, owner.id, "automation.created", {"automation_id": a.id, "confirmed": confirm}, notify=notify)
        return a

    async def get(self, owner_id: str, automation_id: str, *, lock: bool = False) -> Automation:
        q = select(Automation).where(Automation.id == automation_id, Automation.owner_id == owner_id, Automation.deleted_at.is_(None))
        a = await self.db.scalar(q.with_for_update() if lock else q)
        if a is None:
            raise NotFound("automation not found")
        return a

    async def confirm(self, owner: Owner, automation_id: str, expected_version: int) -> Automation:
        a = await self.get(owner.id, automation_id, lock=True)
        self._check_version(a, expected_version)
        if a.confirmed_at is None:
            n = await self.normalize(owner, self._spec_of(a))  # re-validate: a one-time run may have passed since it was proposed
            a.confirmed_at = self.clock.now()
            a.enabled = True
            a.connection_ids = n.values["connection_ids"]
            a.next_run_at = self._first_run(a)
            a.version += 1
            await emit(self.db, owner.id, "automation.confirmed", {"automation_id": a.id})
        return a

    async def update(self, owner: Owner, automation_id: str, expected_version: int, changes: dict[str, Any]) -> Automation:
        a = await self.get(owner.id, automation_id, lock=True)
        self._check_version(a, expected_version)
        enabled = changes.pop("enabled", None)
        if changes:
            merged = self._spec_of(a).model_dump() | changes
            n = await self.normalize(owner, AutomationIn(**merged))
            schedule_changed = any(n.values[k] != getattr(a, k) for k in ("kind", "time_zone", "local_time", "weekdays", "run_date", "trigger"))
            for k, v in n.values.items():
                setattr(a, k, v)
            if schedule_changed:
                a.trigger_state = {}
                a.completed_at = None
                if a.confirmed_at is not None:
                    a.next_run_at = self._first_run(a)
            if a.paused_reason and not await self.connection_problem(a):
                a.paused_reason = None
        if enabled is not None and enabled != a.enabled:
            if enabled and a.confirmed_at is None:
                raise Conflict("confirm this automation before enabling it", code="unconfirmed")
            a.enabled = enabled
            if enabled:
                a.next_run_at = self._first_run(a)
                if a.next_run_at is None:
                    raise Invalid("this one-time automation has already run; change its date to run it again", code="invalid_schedule")
                a.completed_at = None
                if a.paused_reason and not await self.connection_problem(a):
                    a.paused_reason = None
        a.version += 1
        await emit(self.db, owner.id, "automation.updated", {"automation_id": a.id})
        return a

    async def delete(self, owner: Owner, automation_id: str) -> None:
        """DATA-01: disable the schedule, cancel its unfinished runs, and invalidate their proposals."""
        a = await self.get(owner.id, automation_id, lock=True)
        a.deleted_at = self.clock.now()
        a.enabled = False
        a.next_run_at = None
        for occ in await self.db.scalars(select(Occurrence).where(Occurrence.automation_id == a.id, Occurrence.state == "pending")):
            occ.state = "cancelled"
        for task in await self.db.scalars(select(Task).where(Task.origin["automation_id"].astext == a.id, Task.state.notin_(TERMINAL_STATES))):
            await invalidate_actions(self.db, await open_actions_for_task(self.db, task.id), "the automation was deleted", self.clock)
            await transition(self.db, task, "cancelled", self.clock)
        await emit(self.db, owner.id, "automation.deleted", {"automation_id": a.id})

    @staticmethod
    def _check_version(a: Automation, expected: int) -> None:
        if a.version != expected:
            raise Conflict("this automation changed; reload it and try again", code="version_conflict")

    @staticmethod
    def _spec_of(a: Automation) -> AutomationIn:
        cal = CalendarTriggerIn(connection_id=a.trigger["connection_id"], calendar_id=a.trigger["calendar_id"]) if a.trigger else None
        return AutomationIn(name=a.name, kind=a.kind, prompt=a.prompt, time_zone=a.time_zone, local_time=a.local_time,
                            weekdays=a.weekdays, run_date=a.run_date, connection_ids=list(a.connection_ids), calendar=cal,
                            conversation_id=a.conversation_id)

    async def connection_problem(self, a: Automation) -> str | None:
        for cid in a.connection_ids:
            conn = await self.db.get(Connection, cid)
            if conn is None or conn.status == "disconnected":
                return "connection_disconnected"
            if conn.status == "reauth_required":
                return "reauth_required"
            if conn.status != "connected":
                return "connection_error"
        return None


async def pause_for_connection(db: AsyncSession, owner_id: str, connection_id: str, reason: str) -> int:
    """CON-01: pause automations that depend on a connection that was disconnected or needs reconnecting."""
    n = 0
    rows = await db.scalars(select(Automation).where(Automation.owner_id == owner_id, Automation.deleted_at.is_(None),
                                                      connection_id == any_(Automation.connection_ids)).with_for_update())
    for a in rows:
        if a.paused_reason == reason:
            continue
        a.paused_reason = reason
        await emit(db, owner_id, "automation.paused", {"automation_id": a.id, "reason": reason},
                   notify=("Automation paused", f"{a.name} is paused because an account it uses was {'disconnected' if reason == 'connection_disconnected' else 'signed out'}."))
        n += 1
    return n
