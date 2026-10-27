"""DATA-01 export: a ZIP of the owner's data with versioned JSON schemas.

Every entity is exported through an explicit column allowlist, so a new secret column can never
leak into an export by default. Tokens, password hashes, sessions, OAuth state, provider cursors,
and deployment secrets are never read.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, BigInteger, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from opa.artifacts.storage import ArtifactStore
from opa.core.canonical import sha256_hex
from opa.core.clock import Clock
from opa.core.settings import Settings
from opa.db.models import (Action, Approval, Artifact, ArtifactVersion, AuditLog, Automation, BrowserSession, Connection, ConnectionRequest, Conversation, Share,
                           Export, Memory, Message, Notification, Occurrence, Owner, Task)

EXPORT_VERSION = "1"
EXCLUDED = ["password hash", "sessions and cookies", "OAuth state and PKCE verifiers", "provider tokens and the credential vault",
            "calendar sync cursors and trigger baselines", "deployment secrets", "task checkpoints (internal format; outputs are included)"]

# entity -> (model, allowlisted columns)
ENTITIES: dict[str, tuple[Any, list[str]]] = {
    "profile": (Owner, ["email", "time_zone", "assistant_name", "response_style", "preferences", "created_at"]),
    "conversations": (Conversation, ["id", "title", "is_default", "created_at", "updated_at"]),
    "messages": (Message, ["id", "conversation_id", "seq", "role", "content", "task_id", "artifact_ids", "author_user_id", "created_at"]),
    "tasks": (Task, ["id", "conversation_id", "intent", "state", "plan", "usage", "blocker_code", "blocker_detail", "retry_of", "origin",
                     "allowed_connection_ids", "created_at", "finished_at"]),
    "actions": (Action, ["id", "task_id", "connection_id", "tool_name", "effect", "state", "preview", "provider_refs", "resolution",
                         "error", "created_at", "dispatch_finished_at"]),
    "approvals": (Approval, ["id", "action_id", "task_id", "state", "expires_at", "decided_at", "reason", "created_at"]),
    "memory": (Memory, ["id", "text", "kind", "scope", "source_ref", "confirmed", "tags", "version", "created_at", "updated_at"]),
    "artifacts": (Artifact, ["id", "name", "mime", "origin", "task_id", "current_version", "created_at", "updated_at"]),
    "automations": (Automation, ["id", "name", "kind", "prompt", "time_zone", "local_time", "weekdays", "run_date", "trigger", "connection_ids",
                                 "enabled", "confirmed_at", "created_by", "paused_reason", "next_run_at", "last_run_at", "completed_at", "created_at"]),
    "automation_runs": (Occurrence, ["automation_id", "scheduled_for", "state", "task_id", "late", "missed_count", "created_at"]),
    "connections": (Connection, ["id", "provider", "account_label", "status", "modes", "granted_scopes", "authorized_at", "created_at"]),
    "connection_requests": (ConnectionRequest, ["id", "conversation_id", "task_id", "action", "provider", "connection_id", "modes", "reason",
                                                "status", "result", "expires_at", "decided_at", "created_at"]),
    "shares": (Share, ["id", "conversation_id", "title", "snapshot", "message_count", "created_at", "expires_at", "revoked_at", "view_count",
                       "last_viewed_at"]),  # never the token hash
    "browser_sessions": (BrowserSession, ["id", "task_id", "state", "control", "urls_visited", "created_at", "ended_at"]),
    "notifications": (Notification, ["id", "type", "title", "body", "task_id", "read_at", "created_at"]),
    "activity_log": (AuditLog, ["id", "actor", "type", "subject_id", "sanitized", "created_at"]),
}
VERSION_COLUMNS = ["artifact_id", "version", "size", "sha256", "source_refs", "note", "created_at"]


def _json_type(col) -> dict[str, Any]:
    t = col.type
    nullable = col.nullable
    if isinstance(t, DateTime):
        base: dict[str, Any] = {"type": "string", "format": "date-time"}
    elif isinstance(t, Boolean):
        base = {"type": "boolean"}
    elif isinstance(t, (Integer, BigInteger)):
        base = {"type": "integer"}
    elif isinstance(t, ARRAY):
        base = {"type": "array"}
    elif isinstance(t, JSONB):
        base = {}
    else:
        base = {"type": "string"}
    if nullable and "type" in base:
        base = {**base, "type": [base["type"], "null"]}
    return base


def schema_for(name: str, model: Any, cols: list[str]) -> dict[str, Any]:
    table = model.__table__
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"opa-export/v{EXPORT_VERSION}/{name}", "title": name,
            "type": "array", "items": {"type": "object", "additionalProperties": False, "required": cols,
                                       "properties": {c: _json_type(table.c[c]) for c in cols}}}


def _plain(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


async def _rows(db: AsyncSession, owner: Owner, name: str) -> list[dict[str, Any]]:
    model, cols = ENTITIES[name]
    if name == "profile":
        return [{c: _plain(getattr(owner, c)) for c in cols}]
    if name == "messages":
        q = select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(Conversation.owner_id == owner.id, Conversation.deleted_at.is_(None)).order_by(Message.conversation_id, Message.seq)
    elif name == "conversations":
        q = select(Conversation).where(Conversation.owner_id == owner.id, Conversation.deleted_at.is_(None))
    elif name in ("artifacts", "automations"):
        q = select(model).where(model.owner_id == owner.id, model.deleted_at.is_(None))
    else:
        q = select(model).where(model.owner_id == owner.id)
    return [{c: _plain(getattr(r, c)) for c in cols} for r in await db.scalars(q)]


async def build_export(db: AsyncSession, settings: Settings, clock: Clock, owner: Owner) -> Export:
    now = clock.now()
    store = ArtifactStore(settings.artifact_dir)
    buf = io.BytesIO()
    counts: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, (model, cols) in ENTITIES.items():
            rows = await _rows(db, owner, name)
            if name == "tasks":
                outputs = {t.id: (t.checkpoint or {}).get("outputs", {}) for t in await db.scalars(select(Task).where(Task.owner_id == owner.id))}
                for r in rows:
                    r["outputs"] = outputs.get(r["id"], {})
            counts[name] = len(rows)
            z.writestr(f"data/{name}.json", json.dumps(rows, indent=2, ensure_ascii=False, default=str))
            sch = schema_for(name, model, cols)
            if name == "tasks":
                sch["items"]["properties"]["outputs"] = {"type": "object"}
                sch["items"]["required"] = [*cols, "outputs"]
            z.writestr(f"schemas/{name}.schema.json", json.dumps(sch, indent=2))
        versions = []
        arts = {a.id: a for a in await db.scalars(select(Artifact).where(Artifact.owner_id == owner.id, Artifact.deleted_at.is_(None)))}
        for v in await db.scalars(select(ArtifactVersion).where(ArtifactVersion.artifact_id.in_(list(arts) or [""])).order_by(ArtifactVersion.artifact_id, ArtifactVersion.version)):
            path = f"files/{v.artifact_id}/v{v.version}-{arts[v.artifact_id].name}"
            z.writestr(path, store.read_verified(v.storage_key, v.sha256))
            versions.append({**{c: _plain(getattr(v, c)) for c in VERSION_COLUMNS}, "path": path})
        counts["artifact_versions"] = len(versions)
        z.writestr("data/artifact_versions.json", json.dumps(versions, indent=2, default=str))
        vs = schema_for("artifact_versions", ArtifactVersion, VERSION_COLUMNS)
        vs["items"]["properties"]["path"] = {"type": "string"}
        vs["items"]["required"] = [*VERSION_COLUMNS, "path"]
        z.writestr("schemas/artifact_versions.schema.json", json.dumps(vs, indent=2))
        manifest = {"export_version": EXPORT_VERSION, "generated_at": now.isoformat(), "counts": counts, "excluded": EXCLUDED,
                    "entities": {n: {"data": f"data/{n}.json", "schema": f"schemas/{n}.schema.json"} for n in [*ENTITIES, "artifact_versions"]}}
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("README.txt", "Open Personal Agent data export.\nEach data/*.json file is described by the JSON Schema of the same name in schemas/.\n"
                                 "files/ holds every version of your files. Secrets and credentials are not included; see manifest.json.\n")
    data = buf.getvalue()
    folder = Path(settings.export_dir) / owner.id
    folder.mkdir(parents=True, exist_ok=True)
    file_name = f"opa-export-{now.strftime('%Y%m%dT%H%M%SZ')}.zip"
    tmp = folder / f".{file_name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, folder / file_name)
    row = Export(owner_id=owner.id, file_name=file_name, size=len(data), sha256=sha256_hex(data), counts=counts)
    db.add(row)
    await db.flush()
    return row


def export_path(settings: Settings, row: Export) -> Path:
    return Path(settings.export_dir) / row.owner_id / row.file_name
