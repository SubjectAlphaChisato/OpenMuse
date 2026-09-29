"""Worker side of the browser (WEB-01): sessions per task, takeover pauses, and the data-sharing check.

A navigation whose address would carry private context (connector results, file text, memory) to a
website becomes an exact approval, like an email: the owner sees the full address and what it shares.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opa.browser import ops
from opa.browser.client import BrowserClient, BrowserCommandError, BrowserUnavailable
from opa.core.canonical import args_hash
from opa.core.clock import Clock
from opa.db.models import BrowserSession, Job
from opa.db.outbox import emit

ACTIONS = {"browser__navigate": "navigate", "browser__snapshot": "snapshot", "browser__click_link": "click_link",
           "browser__scroll": "scroll", "browser__open_tab": "open_tab", "browser__close_tab": "close_tab"}
PRIVATE_SOURCES = ("gmail__", "calendar__", "artifact__read", "automation__trigger_details")


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "content": json.dumps({"status": "failed", "data": None, "error": {"code": code, "message": message}})}


def private_texts(messages: list[dict[str, Any]], memory_block: str) -> list[str]:
    """Tool results that carry the owner's private data, plus injected memory. Web pages are not private."""
    names: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
    out = [memory_block] if memory_block else []
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and names.get(b.get("tool_use_id"), "").startswith(PRIVATE_SOURCES):
                    out.append(str(b.get("content", "")))
    return out


class BrowserTools:
    names = set(ACTIONS)

    def __init__(self, sm: async_sessionmaker[AsyncSession], clock: Clock, client: BrowserClient | None) -> None:
        self.sm, self.clock, self.client = sm, clock, client

    def definitions(self) -> list[dict[str, Any]]:
        return [dict(t) for t in ops.TOOLS] if self.client else []

    async def _session(self, owner_id: str, task_id: str, token: str) -> BrowserSession:
        async with self.sm() as db, db.begin():
            live = await db.scalar(select(Job.id).where(Job.task_id == task_id, Job.lease_token == token, Job.lease_expires_at > self.clock.now()))
            if live is None:
                raise BrowserCommandError("permission_denied", "worker lease is not current")
            rows = list(await db.scalars(select(BrowserSession).where(BrowserSession.task_id == task_id).order_by(BrowserSession.created_at.desc())))
            if rows and rows[0].stopped_by_owner:
                raise BrowserCommandError("permission_denied", "the owner stopped the browser for this task")
            active = next((r for r in rows if r.state == "active"), None)
            if active is None:
                active = BrowserSession(owner_id=owner_id, task_id=task_id)
                db.add(active)
                await db.flush()
                await emit(db, owner_id, "browser.session_started", {"session_id": active.id}, task_id=task_id)
        await self.client.create(active.id, owner_id, task_id)
        return active

    async def execute(self, name: str, args: dict[str, Any], *, owner_id: str, task_id: str, token: str,
                      messages: list[dict[str, Any]], memory_block: str) -> dict[str, Any]:
        """Returns a tool outcome; {"paused": True} while the owner has control; {"proposal": prep} for data sharing."""
        if self.client is None:
            return _fail("provider_unavailable", "the browser is not configured")
        try:
            sess = await self._session(owner_id, task_id, token)
            if sess.control == "user":
                return {"paused": True}
            action = ACTIONS[name]
            args = dict(args)
            if name in ops.NAVIGATING:
                if action == "click_link":
                    tab = args.get("tab_id")
                    links = (await self._run(sess, "snapshot", {"tab_id": tab})).get("links", [])
                    ref = int(args["ref"])
                    if not 1 <= ref <= len(links):
                        return _fail("invalid_input", f"there is no link {ref} in the latest result")
                    url, action, args = links[ref - 1]["url"], "navigate", {"url": links[ref - 1]["url"], "tab_id": tab}
                else:
                    url = args["url"]
                try:
                    url = ops.check_url(url)
                except ops.UrlRejected as e:
                    return _fail("permission_denied", str(e))
                shared = ops.shared_private_data(url, private_texts(messages, memory_block))
                if shared:
                    canonical = {"url": url, "tab_id": args.get("tab_id"), "new_tab": action == "open_tab", "shared": shared}
                    return {"proposal": {"connection_id": None, "tool_name": "browser.navigate", "tool_version": "1", "effect": "write",
                                         "canonical_args": canonical, "args_hash": args_hash("browser.navigate", "1", canonical),
                                         "preview": ops.preview(canonical), "provider_idempotency_key": None}}
            result = await self._run(sess, action, args)
        except BrowserCommandError as e:
            if e.code == "paused":
                return {"paused": True}
            return _fail(e.code, e.message)
        except BrowserUnavailable:
            return _fail("provider_unavailable", "the browser service is unavailable")
        return await self._record(sess, result)

    async def _run(self, sess: BrowserSession, action: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.client.command(sess.id, action, args)
        except BrowserCommandError as e:
            if e.code != "not_found":
                raise
            await self.client.create(sess.id, sess.owner_id, sess.task_id)  # the service restarted; open a fresh session
            return await self.client.command(sess.id, action, args)

    async def _record(self, sess: BrowserSession, result: dict[str, Any]) -> dict[str, Any]:
        shaped = ops.shape(result)
        if "url" in result and "fetched_at" in result:
            async with self.sm() as db, db.begin():
                row = await db.get(BrowserSession, sess.id, with_for_update=True)
                row.current_url = result["url"]
                row.urls_visited = [*row.urls_visited, {"url": result["url"], "at": result["fetched_at"], "tab_id": result.get("tab_id"),
                                                       "blocked": bool(result.get("blocked")), "title": result.get("title", "")}]
        body = json.dumps({"status": "succeeded", "data": {**shaped, "session_id": sess.id}, "error": None}, ensure_ascii=False)
        out: dict[str, Any] = {"ok": True, "content": body}
        if result.get("url") and not result.get("blocked") and result.get("fetched_at"):
            out["output_key"] = f"web:{result['url']}"
            out["output_value"] = {"url": result["url"], "title": result.get("title", ""), "fetched_at": result["fetched_at"]}
        return out

    async def dispatch_approved(self, canonical: dict[str, Any], *, owner_id: str, task_id: str, token: str) -> dict[str, Any]:
        """Perform an approved data-sharing navigation exactly as approved."""
        sess = await self._session(owner_id, task_id, token)
        if sess.control == "user":
            raise BrowserCommandError("paused", "the owner has taken control of the browser")
        action = "open_tab" if canonical.get("new_tab") else "navigate"
        args = {"url": canonical["url"]} if action == "open_tab" else {"url": canonical["url"], "tab_id": canonical.get("tab_id")}
        return await self._record(sess, await self._run(sess, action, args))

    async def stop_for_task(self, task_id: str) -> None:
        async with self.sm() as db, db.begin():
            rows = list(await db.scalars(select(BrowserSession).where(BrowserSession.task_id == task_id, BrowserSession.state == "active")))
            for r in rows:
                r.state, r.ended_at = "stopped", self.clock.now()
        for r in rows:
            try:
                await self.client.stop(r.id)
            except (BrowserUnavailable, BrowserCommandError):
                pass
