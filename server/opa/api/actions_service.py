"""Invalidate proposals when their preconditions disappear (cancel, disconnect, mode change, deletion)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.api.tasks_service import transition
from opa.core.clock import Clock
from opa.db.models import Action, Approval, Task
from opa.db.outbox import emit

OPEN_ACTION_STATES = ("awaiting_approval", "approved")


async def invalidate_actions(db: AsyncSession, actions: list[Action], reason: str, clock: Clock, *, block_code: str | None = None) -> int:
    n = 0
    for action in actions:
        if action.state not in OPEN_ACTION_STATES:
            continue
        action.state = "cancelled"
        action.error = {"code": "approval_required", "message": reason}
        for ap in await db.scalars(select(Approval).where(Approval.action_id == action.id, Approval.state.in_(("pending", "approved")))):
            ap.state = "invalidated"
            ap.reason = reason
            ap.decided_at = clock.now()
        await emit(db, action.owner_id, "action.cancelled", {"reason": reason}, task_id=action.task_id, action_id=action.id)
        if block_code:
            task = await db.get(Task, action.task_id)
            if task.state == "waiting_approval" and task.checkpoint.get("pending_action", {}).get("action_id") == action.id:
                await transition(db, task, "blocked", clock, blocker_code=block_code, blocker_detail=reason, notify=("Task blocked", reason[:120]))
        n += 1
    return n


async def open_actions_for_task(db: AsyncSession, task_id: str) -> list[Action]:
    return list(await db.scalars(select(Action).where(Action.task_id == task_id, Action.state.in_(OPEN_ACTION_STATES)).with_for_update()))


async def open_actions_for_connection(db: AsyncSession, connection_id: str, capability: str | None = None, effect: str | None = None) -> list[Action]:
    q = select(Action).where(Action.connection_id == connection_id, Action.state.in_(OPEN_ACTION_STATES))
    if capability:
        q = q.where(Action.tool_name.like(f"{capability}.%"))
    if effect:
        q = q.where(Action.effect == effect)
    return list(await db.scalars(q.with_for_update()))
