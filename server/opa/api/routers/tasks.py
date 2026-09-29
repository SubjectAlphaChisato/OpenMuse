from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from opa.api.actions_service import invalidate_actions, open_actions_for_task
from opa.api.deps import ClockDep, DbDep, OwnerDep, SettingsDep, get_owned
from opa.api.schemas import RetryOut, TaskList, TaskOut
from opa.api.tasks_service import add_message, create_task, requeue, task_outputs, transition
from opa.core.errors import Conflict
from opa.db.models import TERMINAL_STATES, Action, Conversation, Task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


async def stop_task_browsers(request: Request, db, task_id: str, clock) -> None:
    """TASK-02: cancelling a task ends its browser sessions immediately."""
    from opa.db.models import BrowserSession

    rows = list(await db.scalars(select(BrowserSession).where(BrowserSession.task_id == task_id, BrowserSession.state == "active")))
    for s in rows:
        s.state, s.ended_at = "stopped", clock.now()
        if request.app.state.browser is not None:
            try:
                await request.app.state.browser.stop(s.id)
            except Exception:  # noqa: BLE001 - the session is closed in the database either way
                pass


def to_out(t: Task) -> TaskOut:
    return TaskOut(
        id=t.id, conversation_id=t.conversation_id, intent=t.intent, state=t.state, plan=t.plan,
        current_step=t.current_step, blocker_code=t.blocker_code, blocker_detail=t.blocker_detail,
        retry_of=t.retry_of, usage=t.usage, active_ms=t.active_ms, version=t.version,
        last_activity_at=t.last_activity_at, finished_at=t.finished_at, created_at=t.created_at,
        outputs=task_outputs(t),
    )


@router.get("", response_model=TaskList)
async def list_tasks(db: DbDep, owner: OwnerDep, state: str | None = None, cursor: str | None = None, limit: int = 50) -> TaskList:
    limit = max(1, min(limit, 200))
    q = select(Task).where(Task.owner_id == owner.id)
    if state:
        q = q.where(Task.state == state)
    if cursor:
        q = q.where(Task.id < cursor)
    rows = list(await db.scalars(q.order_by(Task.id.desc()).limit(limit + 1)))
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return TaskList(items=[to_out(t) for t in rows[:limit]], next_cursor=next_cursor)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, db: DbDep, owner: OwnerDep) -> TaskOut:
    return to_out(await get_owned(db, Task, task_id, owner))


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: str, request: Request, db: DbDep, owner: OwnerDep, clock: ClockDep) -> TaskOut:
    task = await get_owned(db, Task, task_id, owner)
    if task.state in TERMINAL_STATES:
        raise Conflict("task already finished", code="invalid_state")
    await invalidate_actions(db, await open_actions_for_task(db, task.id), "the task was cancelled", clock)
    await transition(db, task, "cancelled", clock)
    await stop_task_browsers(request, db, task.id, clock)
    return to_out(task)


@router.post("/{task_id}/resume", response_model=TaskOut)
async def resume_task(task_id: str, db: DbDep, owner: OwnerDep, clock: ClockDep) -> TaskOut:
    task = await get_owned(db, Task, task_id, owner)
    if task.state not in ("blocked", "waiting_user"):
        raise Conflict("only blocked or waiting_user tasks can be resumed", code="invalid_state")
    unresolved = await db.scalar(select(Action.id).where(Action.task_id == task.id, Action.state.in_(("dispatching", "unknown"))).limit(1))
    if unresolved:
        raise Conflict("an external change has an unknown outcome; check it or mark it before resuming", code="reconcile_required")
    if task.blocker_code in ("budget_exceeded",):
        task.checkpoint = {**task.checkpoint, "budget_extension": task.checkpoint.get("budget_extension", 0) + 1}
    await requeue(db, task, clock)
    return to_out(task)


@router.post("/{task_id}/retry", response_model=RetryOut, status_code=201)
async def retry_task(task_id: str, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> RetryOut:
    task = await get_owned(db, Task, task_id, owner)
    if task.state not in TERMINAL_STATES:
        raise Conflict("only finished tasks can be retried", code="invalid_state")
    conv = await get_owned(db, Conversation, task.conversation_id, owner)
    new = await create_task(db, conv, task.intent, settings, clock, retry_of=task.id)
    confirmed = {k: v for k, v in task_outputs(task).items() if isinstance(v, dict) and v.get("confirmed")}
    new.checkpoint = {"imported_outputs": confirmed}
    await add_message(db, conv, "system", f"Retrying: {task.intent[:200]}", task_id=new.id)
    await db.flush()
    return RetryOut(task_id=new.id)
