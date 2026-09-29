"""HTTP client for the internal broker API, used by the worker and the API."""
from __future__ import annotations

from typing import Any

import httpx

from opa.sdk.contract import ConnectorError, ToolRequest, ToolResult


class BrokerUnavailable(Exception):
    """The broker could not be reached or answered unexpectedly. Callers must not assume anything was dispatched."""


class BrokerClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 150.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, headers={"authorization": f"Bearer {token}"}, transport=transport, timeout=timeout)

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            r = await self._client.post(path, json=body)
        except httpx.HTTPError as e:
            raise BrokerUnavailable(str(e)) from e
        if r.status_code == 400:
            err = r.json().get("error", {})
            raise ConnectorError(err.get("code", "invalid_input"), err.get("message", "broker error"))
        if r.status_code != 200:
            raise BrokerUnavailable(f"broker returned {r.status_code}")
        return r.json()

    async def tools(self, task_id: str, lease_token: str) -> list[dict[str, Any]]:
        return await self._post("/internal/tools", {"task_id": task_id, "lease_token": lease_token})

    async def read(self, req: ToolRequest, lease_token: str) -> ToolResult:
        return ToolResult.model_validate(await self._post("/internal/read", {"request": req.model_dump(), "lease_token": lease_token}))

    async def prepare(self, task_id: str, lease_token: str, connection_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/internal/prepare", {"task_id": task_id, "lease_token": lease_token, "connection_id": connection_id, "tool_name": tool_name, "arguments": arguments})

    async def dispatch(self, action_id: str, task_id: str, lease_token: str) -> ToolResult:
        return ToolResult.model_validate(await self._post("/internal/dispatch", {"action_id": action_id, "task_id": task_id, "lease_token": lease_token}))

    async def reconcile(self, action_id: str, owner_id: str) -> dict[str, Any]:
        return await self._post("/internal/reconcile", {"action_id": action_id, "owner_id": owner_id})

    async def oauth_exchange(self, provider: str, **body: Any) -> dict[str, Any]:
        return await self._post(f"/internal/oauth/{provider}/exchange", body)

    async def google_exchange(self, **body: Any) -> dict[str, Any]:
        return await self.oauth_exchange("google", **body)

    async def revoke(self, connection_id: str, owner_id: str) -> dict[str, Any]:
        return await self._post("/internal/connections/revoke", {"connection_id": connection_id, "owner_id": owner_id})

    async def purge(self, connection_id: str, owner_id: str) -> dict[str, Any]:
        return await self._post("/internal/connections/purge", {"connection_id": connection_id, "owner_id": owner_id})

    async def health(self, connection_id: str, owner_id: str) -> dict[str, Any]:
        return await self._post("/internal/connections/health", {"connection_id": connection_id, "owner_id": owner_id})

    async def calendar_sync(self, owner_id: str, connection_id: str, calendar_id: str, sync_token: str | None, time_min: str) -> dict[str, Any]:
        return await self._post("/internal/calendar/sync", {"owner_id": owner_id, "connection_id": connection_id, "calendar_id": calendar_id,
                                                            "sync_token": sync_token, "time_min": time_min})

    async def aclose(self) -> None:
        await self._client.aclose()
