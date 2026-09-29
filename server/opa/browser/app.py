"""Internal HTTP API of the browser service. Reachable only by the api and worker with the internal token."""
from __future__ import annotations

import asyncio
import hmac
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from opa.browser.service import BrowserError, BrowserService


class CreateIn(BaseModel):
    session_id: str
    owner_id: str
    task_id: str | None = None


class CommandIn(BaseModel):
    action: str
    args: dict[str, Any] = {}


class ControlIn(BaseModel):
    control: str


def create_browser_app(service: BrowserService, token: str, *, reap_interval: float | None = None) -> FastAPI:
    if not token:
        raise RuntimeError("OPA_INTERNAL_TOKEN must be set for the browser service")
    app = FastAPI(title="OPA browser (internal)", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(request: Request) -> None:
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    guarded = [Depends(auth)]
    status = {"not_found": 404, "paused": 409, "permission_denied": 403, "invalid_input": 400, "provider_unavailable": 502}

    @app.exception_handler(BrowserError)
    async def browser_error(_r: Request, e: BrowserError):
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=status.get(e.code, 400))

    @app.get("/internal/health")
    async def health():
        return {"ok": True, "sessions": sum(not s.stopped for s in service.sessions.values())}

    @app.post("/internal/sessions", dependencies=guarded)
    async def create(body: CreateIn):
        return await service.create(body.session_id, body.owner_id, body.task_id)

    @app.get("/internal/sessions/{sid}", dependencies=guarded)
    async def describe(sid: str):
        s = service.get(sid)
        return {**service.describe(s), "visited": s.visited}

    @app.post("/internal/sessions/{sid}/command", dependencies=guarded)
    async def command(sid: str, body: CommandIn):
        return await service.command(sid, body.action, body.args)

    @app.post("/internal/sessions/{sid}/control", dependencies=guarded)
    async def control(sid: str, body: ControlIn):
        if body.control not in ("agent", "user"):
            raise BrowserError("invalid_input", "control must be agent or user")
        return await service.set_control(sid, body.control)

    @app.post("/internal/sessions/{sid}/stop", dependencies=guarded)
    async def stop(sid: str):
        return await service.stop(sid)

    @app.get("/internal/sessions/{sid}/screenshot", dependencies=guarded)
    async def screenshot(sid: str):
        return Response(await service.screenshot(sid), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/internal/sessions/{sid}/input", dependencies=guarded)
    async def owner_input(sid: str, event: dict[str, Any]):
        return await service.owner_input(sid, event)

    if reap_interval:
        @app.on_event("startup")
        async def start_reaper() -> None:
            async def loop() -> None:
                while True:
                    await asyncio.sleep(reap_interval)
                    await service.reap_idle()
            app.state.reaper = asyncio.create_task(loop())

    return app
