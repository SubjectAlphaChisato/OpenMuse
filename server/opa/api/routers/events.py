from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from opa.api.deps import DbDep, OwnerDep, get_owned
from opa.api.schemas import NotificationOut
from opa.db.models import Event, Notification

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events")
async def stream_events(request: Request, owner: OwnerDep, after: int = 0, max_events: int | None = None) -> StreamingResponse:
    """Owner-scoped SSE with replay. Clients resume with ?after=<id> or Last-Event-ID.
    Delivery polls the outbox table (500 ms); the table is the source of truth (UX-01)."""
    last_header = request.headers.get("last-event-id")
    cursor = int(last_header) if last_header and last_header.isdigit() else after
    sessionmaker = request.app.state.sessionmaker
    owner_id = owner.id

    async def gen():
        nonlocal cursor
        yield "retry: 2000\n\n"
        idle = 0
        sent = 0
        while True:
            if max_events is not None and sent >= max_events:
                return
            if await request.is_disconnected():
                return
            async with sessionmaker() as db:
                rows = list(await db.scalars(select(Event).where(Event.owner_id == owner_id, Event.id > cursor).order_by(Event.id).limit(200)))
            for ev in rows:
                cursor = ev.id
                data = json.dumps({"id": ev.id, "type": ev.type, "task_id": ev.task_id, "action_id": ev.action_id, "payload": ev.payload, "created_at": ev.created_at.isoformat()})
                yield f"id: {ev.id}\nevent: {ev.type}\ndata: {data}\n\n"
                sent += 1
            if not rows:
                idle += 1
                if idle % 30 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5)
            else:
                idle = 0

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/events/replay")
async def replay_events(db: DbDep, owner: OwnerDep, after: int = 0, limit: int = 200) -> list[dict]:
    """Non-streaming replay for clients and tests."""
    rows = await db.scalars(select(Event).where(Event.owner_id == owner.id, Event.id > after).order_by(Event.id).limit(max(1, min(limit, 500))))
    return [{"id": e.id, "type": e.type, "task_id": e.task_id, "payload": e.payload, "created_at": e.created_at.isoformat()} for e in rows]


@router.get("/notifications", response_model=list[NotificationOut])
async def list_notifications(db: DbDep, owner: OwnerDep, unread_only: bool = False, limit: int = 100) -> list[NotificationOut]:
    q = select(Notification).where(Notification.owner_id == owner.id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    rows = await db.scalars(q.order_by(Notification.created_at.desc()).limit(max(1, min(limit, 500))))
    return [NotificationOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_read(notification_id: str, request: Request, db: DbDep, owner: OwnerDep) -> NotificationOut:
    n = await get_owned(db, Notification, notification_id, owner)
    if n.read_at is None:
        n.read_at = request.app.state.clock.now()
    return NotificationOut.model_validate(n, from_attributes=True)
