from __future__ import annotations

import logging

from opa import __version__
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import async_sessionmaker

from opa.api.routers import approvals, auth, connections, conversations, events, oauth, tasks
from opa.api.routers import automations as automations_router
from opa.api.routers import browser as browser_router
from opa.api.routers import data as data_router
from opa.api.routers import skills as skills_router  # v0.2 harness
from opa.api.routers import shares as shares_router  # v0.2 sharing
from opa.api.routers.artifacts import router as artifacts_router  # M4
from opa.api.routers.memory import router as memory_router  # M4
from opa.core.clock import Clock, SystemClock
from opa.core.errors import ApiError
from opa.core.settings import Settings, get_settings
from opa.db.engine import get_sessionmaker

log = logging.getLogger("opa.api")


def create_app(settings: Settings | None = None, clock: Clock | None = None, sessionmaker: async_sessionmaker | None = None, broker=None, browser=None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="OpenMuse API", version=__version__, openapi_url="/api/openapi.json", docs_url="/api/docs")
    app.state.settings = settings
    app.state.clock = clock or SystemClock()
    app.state.sessionmaker = sessionmaker or get_sessionmaker()
    if broker is None and settings.internal_token:
        from opa.broker.client import BrokerClient

        broker = BrokerClient(settings.broker_url, settings.internal_token)
    app.state.broker = broker
    if browser is None and settings.browser_url and settings.internal_token:
        from opa.browser.client import BrowserClient

        browser = BrowserClient(settings.browser_url, settings.internal_token)
    app.state.browser = browser

    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def _err(request: Request, status: int, code: str, message: str) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        return JSONResponse({"error": {"code": code, "message": message, "request_id": rid}}, status_code=status)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return _err(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _err(request, 422, "invalid_input", "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        return _err(request, 500, "internal_error", "internal error")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(tasks.router)
    app.include_router(events.router)
    app.include_router(connections.router)
    app.include_router(approvals.router)
    app.include_router(oauth.router)
    app.include_router(automations_router.router)
    app.include_router(skills_router.router)
    app.include_router(shares_router.router)
    app.include_router(shares_router.public)  # /s/{token}: before the web app's catch-all
    app.include_router(browser_router.router)
    app.include_router(data_router.router)
    app.include_router(artifacts_router)
    app.include_router(memory_router)

    if settings.web_dist_dir and Path(settings.web_dist_dir, "index.html").is_file():
        dist = Path(settings.web_dist_dir).resolve()
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                return _err_plain(404)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def _err_plain(status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": "not_found", "message": "not found", "request_id": "-"}}, status_code=status)
