"""Emit events and notifications inside the caller's transaction."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from opa.db.models import Event, Notification


async def emit(
    session: AsyncSession,
    owner_id: str,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    task_id: str | None = None,
    action_id: str | None = None,
    notify: tuple[str, str] | None = None,
) -> Event:
    ev = Event(owner_id=owner_id, type=type, task_id=task_id, action_id=action_id, payload=payload or {})
    session.add(ev)
    await session.flush()
    if notify:
        title, body = notify
        session.add(
            Notification(owner_id=owner_id, event_id=ev.id, type=type, title=title, body=body, task_id=task_id)
        )
        await session.flush()
    return ev
