"""Task lifecycle operations shared by API routers and the worker."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.core.clock import Clock
from opa.core.errors import Conflict
from opa.core.settings import Settings
from opa.db.models import TASK_TRANSITIONS, TERMINAL_STATES, Conversation, Job, Message, Task
from opa.db.outbox import emit


def assert_transition(task: Task, new_state: str) -> None:
    if new_state not in TASK_TRANSITIONS[task.state]:
        raise Conflict(f"cannot move task from {task.state} to {new_state}", code="invalid_state")


async def transition(
    db: AsyncSession,
    task: Task,
    new_state: str,
    clock: Clock,
    *,
    blocker_code: str | None = None,
    blocker_detail: str | None = None,
    notify: tuple[str, str] | None = None,
) -> None:
    assert_transition(task, new_state)
    old = task.state
    task.state = new_state
    task.version += 1
    task.last_activity_at = clock.now()
    if new_state == "blocked":
        task.blocker_code = blocker_code
        task.blocker_detail = blocker_detail
    elif new_state == "queued":
        task.blocker_code = None
        task.blocker_detail = None
    if new_state in TERMINAL_STATES:
        task.finished_at = clock.now()
    await db.flush()
    await emit(
        db,
        task.owner_id,
        f"task.{new_state}",
        {"from": old, "to": new_state, "blocker_code": task.blocker_code},
        task_id=task.id,
        notify=notify,
    )


async def next_seq(db: AsyncSession, conversation_id: str) -> int:
    current = await db.scalar(select(func.max(Message.seq)).where(Message.conversation_id == conversation_id))
    return (current or 0) + 1


async def add_message(
    db: AsyncSession,
    conversation: Conversation,
    role: str,
    content: str,
    *,
    task_id: str | None = None,
    client_request_id: str | None = None,
    content_hash: str | None = None,
    artifact_ids: list[str] | None = None,
) -> Message:
    msg = Message(
        owner_id=conversation.owner_id,
        conversation_id=conversation.id,
        seq=await next_seq(db, conversation.id),
        role=role,
        content=content,
        task_id=task_id,
        client_request_id=client_request_id,
        content_hash=content_hash,
        artifact_ids=artifact_ids or [],
    )
    db.add(msg)
    await db.flush()
    await emit(db, conversation.owner_id, "message.created", {"message_id": msg.id, "conversation_id": conversation.id, "role": role}, task_id=task_id)
    return msg


async def create_task(
    db: AsyncSession, conversation: Conversation, intent: str, settings: Settings, clock: Clock, *, retry_of: str | None = None
) -> Task:
    task = Task(
        owner_id=conversation.owner_id,
        conversation_id=conversation.id,
        intent=intent,
        state="queued",
        limits={
            "max_model_calls": settings.task_max_model_calls,
            "max_tool_dispatches": settings.task_max_tool_dispatches,
            "max_active_seconds": settings.task_max_active_seconds,
            "tool_timeout_seconds": settings.tool_timeout_seconds,
        },
        usage={"model_calls": 0, "tool_dispatches": 0, "input_tokens": 0, "output_tokens": 0},
        retry_of=retry_of,
        last_activity_at=clock.now(),
    )
    db.add(task)
    await db.flush()
    db.add(Job(task_id=task.id, ready_at=clock.now()))
    await db.flush()
    await emit(db, task.owner_id, "task.queued", {"from": None, "to": "queued"}, task_id=task.id)
    return task


async def requeue(db: AsyncSession, task: Task, clock: Clock) -> None:
    """Move a task back to queued and make its job claimable now."""
    await transition(db, task, "queued", clock)
    job = await db.scalar(select(Job).where(Job.task_id == task.id))
    assert job is not None
    job.ready_at = clock.now()
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None
    job.version += 1
    await db.flush()


def task_outputs(task: Task) -> dict[str, Any]:
    return task.checkpoint.get("outputs", {})


def utc(dt: datetime | None) -> datetime | None:
    return dt
