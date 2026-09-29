"""Owner browser controls (WEB-01, UX-01): sessions, visited URLs, live view, take control, resume, stop."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select

from opa.api.auth import SESSION_COOKIE, resolve_session
from opa.api.deps import ClockDep, DbDep, OwnerDep, get_owned
from opa.api.tasks_service import requeue
from opa.browser.client import BrowserCommandError, BrowserUnavailable
from opa.core.errors import Conflict, Invalid
from opa.db.models import BrowserSession, Task
from opa.db.outbox import emit

router = APIRouter(prefix="/api/browser-sessions", tags=["browser"])


class SessionOut(BaseModel):
    id: str
    task_id: str | None
    task_intent: str | None
    state: str
    control: str
    current_url: str | None
    urls_visited: list[dict[str, Any]]
    version: int
    created_at: datetime
    ended_at: datetime | None


async def _out(db, s: BrowserSession) -> SessionOut:
    intent = (await db.get(Task, s.task_id)).intent if s.task_id else None
    return SessionOut(id=s.id, task_id=s.task_id, task_intent=intent, state=s.state, control=s.control, current_url=s.current_url,
                      urls_visited=s.urls_visited, version=s.version, created_at=s.created_at, ended_at=s.ended_at)


def _client(request: Request):
    client = request.app.state.browser
    if client is None:
        raise Invalid("the browser is not configured (OPA_BROWSER_URL)", code="not_configured")
    return client


async def _service(call):
    try:
        return await call
    except BrowserCommandError as e:
        raise Conflict(e.message, code=e.code) from None
    except BrowserUnavailable:
        raise Invalid("the browser service is unavailable", code="provider_unavailable") from None


@router.get("", response_model=list[SessionOut])
async def list_sessions(db: DbDep, owner: OwnerDep, limit: int = 20) -> list[SessionOut]:
    rows = await db.scalars(select(BrowserSession).where(BrowserSession.owner_id == owner.id).order_by(BrowserSession.created_at.desc()).limit(max(1, min(limit, 100))))
    return [await _out(db, s) for s in rows]


@router.post("", response_model=SessionOut, status_code=201)
async def open_session(request: Request, db: DbDep, owner: OwnerDep) -> SessionOut:
    """A browser the owner drives (e.g. to finish something that needs a login). The agent has no access to it."""
    s = BrowserSession(owner_id=owner.id, task_id=None, control="user")
    db.add(s)
    await db.flush()
    await _service(_client(request).create(s.id, owner.id, None))
    await _service(_client(request).control(s.id, "user"))
    return await _out(db, s)


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: str, db: DbDep, owner: OwnerDep) -> SessionOut:
    return await _out(db, await get_owned(db, BrowserSession, session_id, owner))


async def _set_control(request: Request, db, owner, session_id: str, control: str, clock) -> BrowserSession:
    s = await db.scalar(select(BrowserSession).where(BrowserSession.id == session_id, BrowserSession.owner_id == owner.id).with_for_update())
    if s is None or s.state != "active":
        raise Conflict("this browser session has ended", code="invalid_state")
    if control == "agent" and s.task_id is None:
        raise Conflict("this is your own browser; there is no task to hand it back to", code="invalid_state")
    await _service(_client(request).control(s.id, control))
    s.control = control
    s.version += 1
    await emit(db, owner.id, "browser.control", {"session_id": s.id, "control": control}, task_id=s.task_id)
    if control == "agent" and s.task_id:
        task = await db.get(Task, s.task_id)
        if task.state == "waiting_user" and task.checkpoint.get("waiting_for") == "browser_resume":
            task.checkpoint = {**task.checkpoint, "waiting_for": None, "pending_user_reply": "I am done with the browser; continue."}
            await requeue(db, task, clock)
    return s


@router.post("/{session_id}/takeover", response_model=SessionOut)
async def takeover(session_id: str, request: Request, db: DbDep, owner: OwnerDep, clock: ClockDep) -> SessionOut:
    return await _out(db, await _set_control(request, db, owner, session_id, "user", clock))


@router.post("/{session_id}/resume", response_model=SessionOut)
async def resume(session_id: str, request: Request, db: DbDep, owner: OwnerDep, clock: ClockDep) -> SessionOut:
    """Explicit hand-back. Cookies and site storage from the owner's turn are cleared first."""
    return await _out(db, await _set_control(request, db, owner, session_id, "agent", clock))


@router.post("/{session_id}/stop", response_model=SessionOut)
async def stop(session_id: str, request: Request, db: DbDep, owner: OwnerDep, clock: ClockDep) -> SessionOut:
    """Closes the browser. The task may not open another one; it is told the owner stopped the browser."""
    s = await get_owned(db, BrowserSession, session_id, owner)
    if s.state == "active":
        try:
            await _client(request).stop(s.id)
        except (BrowserUnavailable, BrowserCommandError):
            pass
        s.state, s.stopped_by_owner, s.ended_at = "stopped", True, clock.now()
        s.version += 1
        await emit(db, owner.id, "browser.stopped", {"session_id": s.id}, task_id=s.task_id)
        if s.task_id:
            task = await db.get(Task, s.task_id)
            if task.state == "waiting_user" and task.checkpoint.get("waiting_for") == "browser_resume":
                task.checkpoint = {**task.checkpoint, "waiting_for": None, "pending_user_reply": "I stopped the browser. Continue without it."}
                await requeue(db, task, clock)
    return await _out(db, s)


@router.get("/{session_id}/screenshot")
async def screenshot(session_id: str, request: Request, db: DbDep, owner: OwnerDep) -> Response:
    s = await get_owned(db, BrowserSession, session_id, owner)
    if s.state != "active":
        raise Conflict("this browser session has ended", code="invalid_state")
    data = await _service(_client(request).screenshot(s.id))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/{session_id}/input")
async def owner_input(session_id: str, event: dict[str, Any], request: Request, db: DbDep, owner: OwnerDep) -> dict[str, Any]:
    s = await get_owned(db, BrowserSession, session_id, owner)
    if s.state != "active" or s.control != "user":
        raise Conflict("take control before sending input", code="paused")
    return await _service(_client(request).owner_input(s.id, event))


@router.websocket("/{session_id}/stream")
async def stream(websocket: WebSocket, session_id: str) -> None:
    """Live view: about three JPEG frames a second, plus owner input while the owner has control.
    Same cookie session and ownership check as every route; the Origin must be this site (no cross-site sockets)."""
    app = websocket.app
    origin, host = websocket.headers.get("origin", ""), websocket.headers.get("host", "")
    if not origin or origin.split("://", 1)[-1] != host:
        await websocket.close(code=4403)
        return
    async with app.state.sessionmaker() as db:
        sess = await resolve_session(db, app.state.clock, websocket.cookies.get(SESSION_COOKIE))
        row = await db.get(BrowserSession, session_id) if sess else None
    if sess is None or row is None or row.owner_id != sess.owner_id:
        await websocket.close(code=4404)
        return
    client = app.state.browser
    if client is None:
        await websocket.close(code=4503)
        return
    await websocket.accept()

    async def frames() -> None:
        while True:
            async with app.state.sessionmaker() as db:
                cur = await db.get(BrowserSession, session_id)
            if cur is None or cur.state != "active":
                await websocket.send_text(json.dumps({"t": "ended"}))
                return
            try:
                jpeg = await client.screenshot(session_id)
                await websocket.send_text(json.dumps({"t": "frame", "control": cur.control, "url": cur.current_url,
                                                      "jpeg": base64.b64encode(jpeg).decode()}))
            except (BrowserUnavailable, BrowserCommandError):
                await websocket.send_text(json.dumps({"t": "unavailable"}))
            await asyncio.sleep(0.33)

    async def inputs() -> None:
        while True:
            event = json.loads(await websocket.receive_text())
            async with app.state.sessionmaker() as db:
                cur = await db.get(BrowserSession, session_id)
            if cur is None or cur.control != "user":
                await websocket.send_text(json.dumps({"t": "error", "message": "take control before sending input"}))
                continue
            try:
                await client.owner_input(session_id, event)
            except BrowserCommandError as e:
                await websocket.send_text(json.dumps({"t": "error", "message": e.message}))

    tasks = [asyncio.create_task(frames()), asyncio.create_task(inputs())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
