"""Connector broker (design section 6.3, 7). Sole path from the agent to provider operations.

Every call re-derives the owner from the task, re-checks the task lease (fencing), connection
status, access mode, and for writes the approval bound to the exact canonical payload.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opa.broker import attachments as attach
from opa.broker import ops
from opa.broker.fixture import FixtureBackend
from opa.broker.google import scopes as gscopes
from opa.broker.google.adapter import GoogleBackend, ReauthRequired
from opa.broker.google.oauth import GoogleOAuth
from opa.broker.vault import Vault
from opa.core.canonical import args_hash
from opa.core.clock import Clock
from opa.core.settings import Settings
from opa.db.models import Action, Approval, AuditLog, Connection, DispatchClaim, Job, Owner, Task
from opa.db.outbox import emit
from opa.sdk.contract import ConnectorError, OutcomeUnknown, SyncTokenExpired, ToolRequest, ToolResult

log = logging.getLogger("opa.broker")


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    owner_id: str
    approval_mode: str
    allowed_connection_ids: tuple[str, ...] | None = None  # None = any of the owner's connections

    def allows(self, connection_id: str) -> bool:
        return self.allowed_connection_ids is None or connection_id in self.allowed_connection_ids


class Broker:
    def __init__(self, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession], clock: Clock, *, read_retry_base: float = 0.5,
                 google_transport: Any = None) -> None:
        self.settings = settings
        self.sm = sessionmaker
        self.clock = clock
        self.read_retry_base = read_retry_base
        self.google_transport = google_transport
        self.vault = Vault(settings.broker_kek) if settings.broker_kek else None

    # ------------------------------------------------------------ helpers
    def backend(self, conn: Connection):
        if conn.provider == "fixture":
            if self.settings.mode != "demo":
                raise ConnectorError("permission_denied", "fixture connections are disabled in live mode")
            return FixtureBackend(self.sm, conn.id, self.clock, conn.account_label)
        if conn.provider == "google":
            if self.settings.mode != "live":
                raise ConnectorError("permission_denied", "demo mode never contacts real providers")
            if self.vault is None:
                raise ConnectorError("auth_required", "the broker has no key-encryption key (OPA_BROKER_KEK)")
            return GoogleBackend(self.settings, self.sm, self.vault, conn.id, self.clock, self.google_transport, on_reauth=self._mark_reauth)
        raise ConnectorError("auth_required", f"provider {conn.provider} is not available")

    def _provider_allowed(self, provider: str) -> bool:
        return (provider == "fixture") == (self.settings.mode == "demo")

    @staticmethod
    def _missing_scopes(conn: Connection, tool: ops.ToolDefinition) -> list[str]:
        if conn.provider != "google":
            return []
        eff = gscopes.effective(conn.granted_scopes)
        return [s for s in tool.required_scopes if s not in eff]

    async def _mark_reauth(self, connection_id: str) -> None:
        async with self.sm() as db, db.begin():
            conn = await db.get(Connection, connection_id, with_for_update=True)
            if conn is None or conn.status in ("reauth_required", "disconnected"):
                return
            conn.status = "reauth_required"
            conn.last_error = "Google access was revoked or expired"
            conn.version += 1
            await self._audit(db, conn.owner_id, "connection.reauth_required", conn.id, {"provider": conn.provider})
            await emit(db, conn.owner_id, "connection.reauth_required", {"connection_id": conn.id},
                       notify=("Reconnect your Google account", f"Access for {conn.account_label} expired or was revoked. Reconnect it to continue."))

    async def _task_context(self, db: AsyncSession, task_id: str, lease_token: str) -> "TaskContext":
        # Column-limited reads: the broker role may not read task content or owner credentials.
        row = (await db.execute(select(Task.id, Task.owner_id, Task.state, Task.allowed_connection_ids).where(Task.id == task_id))).first()
        if row is None:
            raise ConnectorError("not_found", "task not found")
        if row.state != "running":
            raise ConnectorError("permission_denied", f"task is {row.state}; no dispatch allowed")
        live = await db.scalar(select(Job.id).where(Job.task_id == task_id, Job.lease_token == lease_token, Job.lease_expires_at > self.clock.now()))
        if live is None:
            raise ConnectorError("permission_denied", "worker lease is not current")
        prefs = await db.scalar(select(Owner.preferences).where(Owner.id == row.owner_id)) or {}
        allowed = tuple(row.allowed_connection_ids) if row.allowed_connection_ids is not None else None
        return TaskContext(task_id=row.id, owner_id=row.owner_id, approval_mode=prefs.get("approval_mode", "ask_writes"), allowed_connection_ids=allowed)

    async def _connection(self, db: AsyncSession, owner_id: str, connection_id: str, capability: str, need: str, tool: ops.ToolDefinition | None = None,
                          ctx: TaskContext | None = None) -> Connection:
        conn = await db.get(Connection, connection_id)
        if conn is None or conn.owner_id != owner_id:
            raise ConnectorError("not_found", "connection not found")
        if ctx is not None and not ctx.allows(connection_id):
            raise ConnectorError("permission_denied", f"this task may not use {conn.account_label}; the automation that started it does not include that account")
        if conn.status != "connected":
            raise ConnectorError("auth_required", f"connection is {conn.status}")
        mode = conn.modes.get(capability, "off")
        if mode == "off":
            raise ConnectorError("permission_denied", f"{capability} access is off for {conn.account_label}")
        if need == "write" and mode != "read_write":
            raise ConnectorError("permission_denied", f"{capability} is read-only for {conn.account_label}; the owner must allow read and write first")
        if tool is not None and self._missing_scopes(conn, tool):
            raise ConnectorError("permission_denied", f"Google has not granted {', '.join(self._missing_scopes(conn, tool))} for {conn.account_label}; reconnect to grant it")
        return conn

    async def _audit(self, db: AsyncSession, owner_id: str, type_: str, subject: str, data: dict[str, Any]) -> None:
        db.add(AuditLog(owner_id=owner_id, actor="broker", type=type_, subject_id=subject, sanitized=data))

    # ------------------------------------------------------------ tool listing
    async def list_tools(self, task_id: str, lease_token: str) -> list[dict[str, Any]]:
        async with self.sm() as db:
            ctx = await self._task_context(db, task_id, lease_token)
            conns = [c for c in await db.scalars(select(Connection).where(Connection.owner_id == ctx.owner_id, Connection.status == "connected")) if ctx.allows(c.id)]
        out = []
        for tool in ops.TOOLS.values():
            eligible = []
            for c in conns:
                if not self._provider_allowed(c.provider) or self._missing_scopes(c, tool):
                    continue
                mode = c.modes.get(tool.capability, "off")
                if mode == "read_write" or (mode == "read_only" and tool.effect == "read"):
                    eligible.append(c)
            if not eligible:
                continue
            schema = {**tool.input_schema, "properties": {"connection_id": {"type": "string", "enum": [c.id for c in eligible], "description": "; ".join(f"{c.id} = {c.account_label}" for c in eligible)}, **tool.input_schema["properties"]}}
            schema["required"] = ["connection_id", *tool.input_schema["required"]]
            desc = tool.description + (" Requires the owner's approval." if tool.effect == "write" or ctx.approval_mode == "always_ask" else "")
            out.append({"name": tool.wire_name, "tool_name": tool.name, "version": tool.version, "effect": tool.effect, "description": desc, "input_schema": schema})
        return out

    # ------------------------------------------------------------ reads
    async def read(self, req: ToolRequest, lease_token: str) -> ToolResult:
        tool = ops.get_tool(req.tool_name)
        try:
            if tool.effect != "read":
                raise ConnectorError("approval_required", "writes must be proposed for approval")
            async with self.sm() as db:
                ctx = await self._task_context(db, req.task_id, lease_token)
                if ctx.approval_mode == "always_ask":
                    raise ConnectorError("approval_required", "the owner asked to approve every read")
                conn = await self._connection(db, ctx.owner_id, req.connection_id, tool.capability, "read", tool, ctx=ctx)
                backend = self.backend(conn)
            ops.validate_schema(tool, req.arguments)
            raw = await self._read_with_retry(backend, tool.name, req.arguments)
            data = self._shape_read(tool.name, raw["data"])
        except ConnectorError as e:
            return ToolResult.fail(e.code, e.message, retry_after_seconds=e.retry_after_seconds)
        async with self.sm() as db, db.begin():
            await self._audit(db, ctx.owner_id, "connector.read", req.task_id, {"tool": tool.name, "connection_id": conn.id, "sources": len(raw.get("source_refs", []))})
        return ToolResult(status="succeeded", data=data, source_references=raw.get("source_refs", []))

    async def _read_with_retry(self, backend, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(3):
            try:
                return await asyncio.wait_for(backend.read(tool_name, args), timeout=self.settings.tool_timeout_seconds)
            except ConnectorError as e:
                if e.code not in ("provider_unavailable", "rate_limited") or attempt == 2:
                    raise
                delay = max(e.retry_after_seconds or 0, self.read_retry_base * (2 ** attempt))
                await asyncio.sleep(delay * (0.5 + 0.5 * random.random()))
            except asyncio.TimeoutError:
                if attempt == 2:
                    raise ConnectorError("provider_unavailable", "provider timed out") from None
        raise ConnectorError("provider_unavailable", "unreachable")

    @staticmethod
    def _shape_read(tool_name: str, data: dict[str, Any]) -> dict[str, Any]:
        """Apply GMAIL-01 filtering and mark message text as untrusted."""
        if tool_name == "gmail.search_messages":
            kept, dropped = ops.filter_messages(data["messages"])
            for m in kept:
                m.pop("body", None)
                m["snippet"] = ops.untrusted(m["snippet"], 200)
            return {"messages": kept, "filtered_count": dropped, "next_page_token": data.get("next_page_token")}
        if tool_name == "gmail.get_message":
            m = data["message"]
            if ops.is_sensitive(m["subject"], m["body"]):
                raise ConnectorError("permission_denied", "this message looks like a login code or password reset and is withheld")
            return {"message": {k: m[k] for k in ("id", "thread_id", "from", "to", "cc", "subject", "date")} | {"body": ops.untrusted(m["body"])}}
        if tool_name == "gmail.get_thread":
            kept, dropped = ops.filter_messages(data["messages"])
            return {"thread_id": data["thread_id"], "filtered_count": dropped,
                    "messages": [{k: m[k] for k in ("id", "from", "to", "subject", "date")} | {"body": ops.untrusted(m["body"])} for m in kept]}
        return data

    # ------------------------------------------------------------ proposals
    async def prepare(self, task_id: str, lease_token: str, connection_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate a proposed action and return its canonical payload. Creates nothing."""
        tool = ops.get_tool(tool_name)
        async with self.sm() as db:
            ctx = await self._task_context(db, task_id, lease_token)
            if tool.effect == "read" and ctx.approval_mode != "always_ask":
                raise ConnectorError("invalid_input", "reads do not need approval; call the tool directly")
            conn = await self._connection(db, ctx.owner_id, connection_id, tool.capability, tool.effect, tool, ctx=ctx)
            backend = self.backend(conn)
            files = await attach.resolve(db, ctx.owner_id, arguments.get("attachments") or [], conn.provider) if tool.name == "gmail.send_message" else None
        now = self.clock.now()
        if tool.effect == "read":
            ops.validate_schema(tool, arguments)
            canonical, key = dict(arguments), None
        else:
            current = None
            if tool.name == "calendar.update_event":
                ops.validate_schema(tool, arguments)
                current = await backend.get_event(arguments["calendar_id"], arguments["event_id"])
            canonical, key = ops.canonicalize_write(tool, arguments, now, account=conn.account_label, current=current, attachments=files)
        return {
            "tool_name": tool.name, "tool_version": tool.version, "effect": tool.effect, "connection_id": conn.id,
            "canonical_args": canonical, "args_hash": args_hash(tool.name, tool.version, canonical),
            "preview": ops.preview(tool.name, canonical, account=conn.account_label), "provider_idempotency_key": key,
        }

    # ------------------------------------------------------------ ledger dispatch
    async def dispatch(self, action_id: str, task_id: str, lease_token: str) -> ToolResult:
        now = self.clock.now()
        async with self.sm() as db:
            async with db.begin():
                action = await db.scalar(select(Action).where(Action.id == action_id).with_for_update())
                if action is None or action.task_id != task_id:
                    return ToolResult.fail("not_found", "action not found")
                if action.state in ("dispatching", "unknown", "succeeded", "failed"):
                    return self._existing(action)
                if action.state != "approved":
                    return ToolResult.fail("approval_required", f"action is {action.state}")
                approval = await db.scalar(select(Approval).where(Approval.action_id == action.id, Approval.state == "approved"))
                tool = ops.get_tool(action.tool_name)
                recomputed = args_hash(action.tool_name, action.tool_version, action.canonical_args)
                # M4: approved attachment bytes must be unchanged (exists, still current, same SHA-256 on disk).
                changed = await attach.verify(db, self.settings, action.owner_id, action.canonical_args.get("attachments") or [])
                if approval is None or not (recomputed == action.args_hash == approval.args_hash) or changed:
                    action.state = "cancelled"
                    action.error = {"code": "approval_required", "message": changed or "payload does not match the approved version"}
                    if approval is not None:
                        approval.state = "invalidated"
                        approval.reason = changed or "payload changed after approval"
                    await self._audit(db, action.owner_id, "action.integrity_violation", action.id, {"tool": action.tool_name})
                    await emit(db, action.owner_id, "action.cancelled", {"reason": "integrity"}, task_id=task_id, action_id=action.id)
                    return ToolResult.fail("approval_required", f"{changed}; a new approval is required" if changed else "the payload changed after approval; a new approval is required")
                if approval.expires_at <= now:
                    action.state, approval.state = "expired", "expired"
                    return ToolResult.fail("approval_required", "the approval expired")
                try:
                    ctx = await self._task_context(db, task_id, lease_token)
                    if ctx.owner_id != action.owner_id:
                        raise ConnectorError("not_found", "action not found")
                    conn = await self._connection(db, ctx.owner_id, action.connection_id, tool.capability, action.effect, tool, ctx=ctx)
                    backend = self.backend(conn)
                except ConnectorError as e:
                    return ToolResult.fail(e.code, e.message)
                claimed = await db.scalar(
                    pg_insert(DispatchClaim).values(action_id=action.id, worker_lease_token=lease_token).on_conflict_do_nothing().returning(DispatchClaim.action_id)
                )
                if claimed is None:
                    return self._existing(action)
                action.state = "dispatching"
                action.dispatch_started_at = now
                action.lease_token_at_dispatch = lease_token
                await emit(db, action.owner_id, "action.dispatching", {"tool": action.tool_name}, task_id=task_id, action_id=action.id)
            # ---- network, outside any transaction
            outcome: dict[str, Any]
            try:
                if action.effect == "read":
                    raw = await asyncio.wait_for(backend.read(action.tool_name, action.canonical_args), timeout=self.settings.tool_timeout_seconds)
                    outcome = {"state": "succeeded", "result": self._shape_read(action.tool_name, raw["data"]), "refs": raw.get("source_refs", [])}
                else:
                    raw = await asyncio.wait_for(backend.write(action.tool_name, action.canonical_args, action.provider_idempotency_key), timeout=self.settings.tool_timeout_seconds)
                    outcome = {"state": "succeeded", "result": raw["data"], "refs": raw.get("provider_refs", [])}
            except ConnectorError as e:
                outcome = {"state": "failed", "error": {"code": e.code, "message": e.message}}
            except (OutcomeUnknown, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - any other failure after dispatch is ambiguous
                log.warning("dispatch of %s ended with unknown outcome: %r", action.id, e)
                outcome = {"state": "unknown", "error": {"code": "unknown_outcome", "message": "the provider may or may not have applied this change"}}
            async with db.begin():
                action = await db.get(Action, action_id, with_for_update=True)
                action.state = outcome["state"]
                action.result = outcome.get("result")
                action.error = outcome.get("error")
                action.provider_refs = outcome.get("refs", [])
                action.dispatch_finished_at = self.clock.now()
                await self._audit(db, action.owner_id, f"action.{action.state}", action.id, {"tool": action.tool_name, "refs": action.provider_refs})
                notify = ("Outcome unknown", "Check whether this change happened before continuing") if action.state == "unknown" else None
                await emit(db, action.owner_id, f"action.{action.state}", {"tool": action.tool_name}, task_id=task_id, action_id=action.id, notify=notify)
            return self._existing(action)

    @staticmethod
    def _existing(action: Action) -> ToolResult:
        if action.state == "succeeded":
            return ToolResult(status="succeeded", data=action.result, provider_references=action.provider_refs)
        if action.state == "failed":
            err = action.error or {"code": "provider_unavailable", "message": "failed"}
            return ToolResult.fail(err["code"], err["message"])
        return ToolResult(status="unknown", error={"code": "unknown_outcome", "message": "outcome not yet known"})

    # ------------------------------------------------------------ reconciliation (design 6.4)
    async def reconcile(self, action_id: str, owner_id: str) -> dict[str, Any]:
        async with self.sm() as db:
            action = await db.get(Action, action_id)
            if action is None or action.owner_id != owner_id:
                raise ConnectorError("not_found", "action not found")
            if action.state != "unknown":
                return {"state": action.state, "changed": False}
            conn = await db.get(Connection, action.connection_id)
            backend = self.backend(conn)
        c = action.canonical_args
        verdict, refs, note = None, [], ""
        try:
            if action.tool_name == "calendar.create_event":
                ev = await backend.get_event(c["calendar_id"], action.provider_idempotency_key)
                if ev is not None:
                    verdict, refs = "succeeded", [f"calendar:{c['calendar_id']}/{ev['id']}"]
                else:
                    verdict, note = "failed", "no event with the reserved ID exists; it was not created"
            elif action.tool_name == "gmail.send_message":
                sent = await backend.find_sent(c["message_id_header"])
                if sent is not None:
                    verdict, refs = "succeeded", [f"gmail:{sent['id']}"]
                else:
                    note = "not found in Sent mail; check your Sent folder, then mark it sent or not sent"
            elif action.tool_name == "calendar.update_event":
                ev = await backend.get_event(c["calendar_id"], c["event_id"])
                if ev is not None and ev["etag"] == c["expected_etag"]:
                    verdict, note = "failed", "the event is unchanged; the update was not applied"
                elif ev is not None and all(ev.get(k) == v for k, v in c["changes"].items()):
                    verdict, refs = "succeeded", [f"calendar:{c['calendar_id']}/{ev['id']}"]
                else:
                    note = "the event changed in a way that does not match; check it manually"
        except ConnectorError as e:
            note = f"reconciliation failed: {e.message}"
        if verdict is None:
            return {"state": "unknown", "changed": False, "note": note}
        async with self.sm() as db, db.begin():
            action = await db.get(Action, action_id, with_for_update=True)
            if action.state != "unknown":
                return {"state": action.state, "changed": False}
            action.state = verdict
            action.resolution = "provider_lookup"
            action.provider_refs = refs
            if verdict == "failed":
                action.error = {"code": "not_applied", "message": note}
            else:
                action.result = {"reconciled": True}
            await self._audit(db, owner_id, "action.reconciled", action.id, {"state": verdict})
            await emit(db, owner_id, f"action.{verdict}", {"reconciled": True}, task_id=action.task_id, action_id=action.id)
        return {"state": verdict, "changed": True, "note": note}

    # ------------------------------------------------------------ sweep
    async def sweep_stale_dispatches(self) -> int:
        """A dispatch older than the tool timeout with no recorded result is `unknown`, never retried."""
        cutoff = self.clock.now() - timedelta(seconds=self.settings.tool_timeout_seconds)
        async with self.sm() as db, db.begin():
            rows = list(await db.scalars(select(Action).where(Action.state == "dispatching", Action.dispatch_started_at < cutoff).with_for_update(skip_locked=True)))
            for a in rows:
                a.state = "unknown"
                a.error = {"code": "unknown_outcome", "message": "dispatch was interrupted"}
                await emit(db, a.owner_id, "action.unknown", {"reason": "interrupted"}, task_id=a.task_id, action_id=a.id,
                           notify=("Outcome unknown", "A change was interrupted; check whether it happened"))
        return len(rows)

    # ------------------------------------------------------------ OAuth lifecycle (CON-01)
    async def google_exchange(self, owner_id: str, candidate_id: str, target_id: str | None, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
        if self.vault is None:
            raise ConnectorError("auth_required", "the broker has no key-encryption key (OPA_BROKER_KEK)")
        oauth = GoogleOAuth(self.settings, self.google_transport)
        token = await oauth.exchange(code, code_verifier, redirect_uri)
        ident = await oauth.identity(token["access_token"])
        granted = sorted(set((token.get("scope") or "").split()))
        async with self.sm() as db, db.begin():
            existing = await db.scalar(select(Connection).where(Connection.owner_id == owner_id, Connection.provider == "google",
                                                                  Connection.provider_account_id == ident["sub"], Connection.status != "disconnected"))
            target = await db.get(Connection, target_id) if target_id else None
            if existing is not None:
                connection_id = existing.id
            elif target is not None and target.owner_id == owner_id and target.provider_account_id == ident["sub"]:
                connection_id = target.id
            else:
                connection_id = candidate_id
            secret = {"access_token": token["access_token"], "refresh_token": token["refresh_token"], "scopes": granted,
                      "expires_at": (self.clock.now() + timedelta(seconds=int(token.get("expires_in", 3600)))).isoformat()}
            await self.vault.store(db, connection_id, secret)
            await self._audit(db, owner_id, "connection.authorized", connection_id, {"provider": "google", "scopes": granted})
        return {"connection_id": connection_id, "email": ident["email"], "account_id": ident["sub"], "granted_scopes": granted,
                "is_new": connection_id == candidate_id, "account_mismatch": bool(target_id) and connection_id != target_id}

    async def revoke(self, connection_id: str, owner_id: str) -> dict[str, Any]:
        """Attempt provider revocation, then delete the local credential regardless of the outcome."""
        async with self.sm() as db:
            conn = await db.get(Connection, connection_id)
            if conn is None or conn.owner_id != owner_id:
                raise ConnectorError("not_found", "connection not found")
        if conn.provider != "google" or self.vault is None:
            return {"revoked": conn.provider == "fixture", "error": None if conn.provider == "fixture" else "no credential store"}
        async with self.sm() as db:
            secret = await self.vault.load(db, connection_id)
        revoked, error = (False, "no stored credential")
        if secret:
            revoked, error = await GoogleOAuth(self.settings, self.google_transport).revoke(secret["refresh_token"])
        async with self.sm() as db, db.begin():
            await self.vault.delete(db, connection_id)
            await self._audit(db, owner_id, "connection.revoked" if revoked else "connection.revocation_failed", connection_id, {"error": error})
        return {"revoked": revoked, "error": error}

    async def health(self, connection_id: str, owner_id: str) -> dict[str, Any]:
        async with self.sm() as db:
            conn = await db.get(Connection, connection_id)
            if conn is None or conn.owner_id != owner_id:
                raise ConnectorError("not_found", "connection not found")
        if conn.status == "disconnected":
            return {"status": "disconnected", "detail": "the account is disconnected"}
        detail = None
        try:
            backend = self.backend(conn)
            if isinstance(backend, GoogleBackend):
                await GoogleOAuth(self.settings, self.google_transport).identity(await backend.access_token())
            status = "connected"
        except ReauthRequired:
            return {"status": "reauth_required", "detail": "Google access was revoked or expired; reconnect the account"}
        except ConnectorError as e:
            if e.code == "auth_required":
                await self._mark_reauth(connection_id)
                return {"status": "reauth_required", "detail": e.message}
            return {"status": conn.status, "detail": e.message}
        async with self.sm() as db, db.begin():
            c = await db.get(Connection, connection_id, with_for_update=True)
            c.last_health_at = self.clock.now()
            if c.status in ("reauth_required", "error"):
                c.version += 1
            c.status, c.last_error = status, None
        return {"status": status, "detail": detail}

    # ------------------------------------------------------------ calendar change polling (AUTO-02)
    async def calendar_sync(self, owner_id: str, connection_id: str, calendar_id: str, sync_token: str | None, time_min: str) -> dict[str, Any]:
        """Read calendar changes for the scheduler. Same checks as a read: ownership, status, mode, scopes."""
        tool = ops.get_tool("calendar.list_events")
        async with self.sm() as db:
            conn = await self._connection(db, owner_id, connection_id, tool.capability, "read", tool)
            backend = self.backend(conn)
        reset = False
        try:
            res = await backend.sync_events(calendar_id, sync_token, time_min)
        except SyncTokenExpired:
            reset = True
            res = await backend.sync_events(calendar_id, None, time_min)
        async with self.sm() as db, db.begin():
            await self._audit(db, owner_id, "connector.calendar_sync", connection_id, {"calendar_id": calendar_id, "changes": len(res["events"]), "full_resync": reset})
        return {**res, "full": sync_token is None or reset, "reset": reset}

    async def ping_db(self) -> bool:
        async with self.sm() as db:
            return (await db.scalar(text("select 1"))) == 1
