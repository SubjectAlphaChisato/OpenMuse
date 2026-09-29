"""Client for the internal browser service (used by the worker and the api)."""
from __future__ import annotations

from typing import Any

import httpx


class BrowserUnavailable(Exception):
    pass


class BrowserCommandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class BrowserClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 60.0) -> None:
        self._c = httpx.AsyncClient(base_url=base_url, headers={"authorization": f"Bearer {token}"}, transport=transport, timeout=timeout)

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = await self._c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise BrowserUnavailable(str(e)) from e
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            if not err:
                raise BrowserUnavailable(f"browser service returned {r.status_code}")
            raise BrowserCommandError(err.get("code", "error"), err.get("message", "browser error"))
        return r

    async def create(self, session_id: str, owner_id: str, task_id: str | None) -> dict[str, Any]:
        return (await self._req("POST", "/internal/sessions", json={"session_id": session_id, "owner_id": owner_id, "task_id": task_id})).json()

    async def describe(self, session_id: str) -> dict[str, Any]:
        return (await self._req("GET", f"/internal/sessions/{session_id}")).json()

    async def command(self, session_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        return (await self._req("POST", f"/internal/sessions/{session_id}/command", json={"action": action, "args": args})).json()

    async def control(self, session_id: str, control: str) -> dict[str, Any]:
        return (await self._req("POST", f"/internal/sessions/{session_id}/control", json={"control": control})).json()

    async def stop(self, session_id: str) -> dict[str, Any]:
        return (await self._req("POST", f"/internal/sessions/{session_id}/stop")).json()

    async def screenshot(self, session_id: str) -> bytes:
        return (await self._req("GET", f"/internal/sessions/{session_id}/screenshot")).content

    async def owner_input(self, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return (await self._req("POST", f"/internal/sessions/{session_id}/input", json=event)).json()

    async def aclose(self) -> None:
        await self._c.aclose()
