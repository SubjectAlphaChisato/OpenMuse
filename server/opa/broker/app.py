"""Internal broker HTTP API. Reachable only on the internal network with the shared bearer token.
Never exposed to the browser, and no route accepts a URL or arbitrary HTTP request."""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from opa.broker.service import Broker
from opa.sdk.contract import ConnectorError, ToolRequest, ToolResult

log = logging.getLogger("opa.broker")


class Lease(BaseModel):
    task_id: str
    lease_token: str


class ReadIn(BaseModel):
    request: ToolRequest
    lease_token: str


class PrepareIn(Lease):
    connection_id: str
    tool_name: str
    arguments: dict[str, Any]


class DispatchIn(Lease):
    action_id: str


class ExchangeIn(BaseModel):
    owner_id: str  # supplied by the authenticated API after it validated the OAuth state
    candidate_connection_id: str
    target_connection_id: str | None = None
    code: str
    code_verifier: str
    redirect_uri: str


class OwnedConnection(BaseModel):
    connection_id: str
    owner_id: str


class ReconcileIn(BaseModel):
    action_id: str
    owner_id: str  # supplied by the authenticated API, never by the model


def create_broker_app(broker: Broker, *, sweep_interval: float | None = None) -> FastAPI:
    token = broker.settings.internal_token
    if not token:
        raise RuntimeError("OPA_INTERNAL_TOKEN must be set for the broker")
    app = FastAPI(title="OPA broker (internal)", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.broker = broker

    def auth(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    guarded = [Depends(auth)]

    @app.exception_handler(ConnectorError)
    async def connector_error(_req: Request, exc: ConnectorError):
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=400)

    @app.get("/internal/health")
    async def health():
        return {"ok": await broker.ping_db()}

    @app.post("/internal/tools", dependencies=guarded)
    async def tools(body: Lease) -> list[dict[str, Any]]:
        return await broker.list_tools(body.task_id, body.lease_token)

    @app.post("/internal/read", dependencies=guarded, response_model=ToolResult)
    async def read(body: ReadIn) -> ToolResult:
        return await broker.read(body.request, body.lease_token)

    @app.post("/internal/prepare", dependencies=guarded)
    async def prepare(body: PrepareIn) -> dict[str, Any]:
        return await broker.prepare(body.task_id, body.lease_token, body.connection_id, body.tool_name, body.arguments)

    @app.post("/internal/dispatch", dependencies=guarded, response_model=ToolResult)
    async def dispatch(body: DispatchIn) -> ToolResult:
        return await broker.dispatch(body.action_id, body.task_id, body.lease_token)

    @app.post("/internal/reconcile", dependencies=guarded)
    async def reconcile(body: ReconcileIn) -> dict[str, Any]:
        return await broker.reconcile(body.action_id, body.owner_id)

    @app.post("/internal/oauth/google/exchange", dependencies=guarded)
    async def google_exchange(body: ExchangeIn) -> dict[str, Any]:
        return await broker.google_exchange(body.owner_id, body.candidate_connection_id, body.target_connection_id, body.code, body.code_verifier, body.redirect_uri)

    @app.post("/internal/connections/revoke", dependencies=guarded)
    async def revoke(body: OwnedConnection) -> dict[str, Any]:
        return await broker.revoke(body.connection_id, body.owner_id)

    @app.post("/internal/connections/health", dependencies=guarded)
    async def health_check(body: OwnedConnection) -> dict[str, Any]:
        return await broker.health(body.connection_id, body.owner_id)

    if sweep_interval:
        @app.on_event("startup")
        async def start_sweep() -> None:
            async def loop() -> None:
                while True:
                    try:
                        await broker.sweep_stale_dispatches()
                    except Exception:
                        log.exception("sweep failed")
                    await asyncio.sleep(sweep_interval)
            app.state.sweeper = asyncio.create_task(loop())

    return app
