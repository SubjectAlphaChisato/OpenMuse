"""Artifact metadata, immutable versions, and the rules that tie them to approvals (ART-01).

Used by the API (owner uploads and edits) and by the worker (typed tools). Errors are ApiError
subclasses so the API returns them directly and the worker turns them into tool errors.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opa.api.actions_service import OPEN_ACTION_STATES, invalidate_actions
from opa.api.tasks_service import requeue
from opa.artifacts import render
from opa.artifacts.extract import extract_text
from opa.artifacts.files import (
    KINDS,
    FileRejected,
    check_upload,
    decode_text,
    kind_from_name,
    sanitize_filename,
)
from opa.artifacts.storage import ArtifactStore
from opa.core.clock import Clock
from opa.core.errors import Conflict, Invalid, NotFound
from opa.core.settings import Settings
from opa.db.models import Action, Artifact, ArtifactVersion, AuditLog, Task
from opa.db.outbox import emit

READ_LIMIT = 50_000  # characters of file text handed to the model per read


@dataclass
class Saved:
    artifact: Artifact
    version: ArtifactVersion

    def summary(self) -> dict[str, Any]:
        return {"artifact_id": self.artifact.id, "name": self.artifact.name, "mime": self.artifact.mime,
                "version": self.version.version, "sha256": self.version.sha256, "size": self.version.size}


def rejected(e: FileRejected) -> Invalid:
    status = 413 if e.code == "too_large" else 422
    return Invalid(e.message, code=e.code, status=status)


def ensure_extension(name: str, kind: str) -> str:
    name = sanitize_filename(name, default="untitled")
    if kind_from_name(name) != kind:
        stem = name.rsplit(".", 1)[0] if "." in name else name
        name = f"{stem or 'untitled'}.{kind}"
    return name


class ArtifactService:
    def __init__(self, db: AsyncSession, settings: Settings, clock: Clock) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.store = ArtifactStore(settings.artifact_dir)

    # ------------------------------------------------------------ reads
    async def list(self, owner_id: str, limit: int = 200) -> list[Artifact]:
        q = select(Artifact).where(Artifact.owner_id == owner_id, Artifact.deleted_at.is_(None)).order_by(Artifact.updated_at.desc(), Artifact.id.desc())
        return list(await self.db.scalars(q.limit(limit)))

    async def get(self, owner_id: str, artifact_id: str, *, for_update: bool = False) -> Artifact:
        q = select(Artifact).where(Artifact.id == artifact_id, Artifact.owner_id == owner_id, Artifact.deleted_at.is_(None))
        a = await self.db.scalar(q.with_for_update() if for_update else q)
        if a is None:
            raise NotFound("file not found")
        return a

    async def versions(self, artifact: Artifact) -> list[ArtifactVersion]:
        return list(await self.db.scalars(select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact.id).order_by(ArtifactVersion.version.desc())))

    async def version(self, artifact: Artifact, version: int | None = None) -> ArtifactVersion:
        v = await self.db.get(ArtifactVersion, (artifact.id, version or artifact.current_version))
        if v is None:
            raise NotFound("version not found")
        return v

    def bytes_of(self, v: ArtifactVersion) -> bytes:
        return self.store.read_verified(v.storage_key, v.sha256)

    def text_of(self, artifact: Artifact, v: ArtifactVersion, limit: int = READ_LIMIT) -> tuple[str, bool]:
        if artifact.mime == KINDS["pdf"]:
            text = v.extracted_text or ""
            return text[:limit], len(text) > limit
        return decode_text(self.bytes_of(v), limit)

    # ------------------------------------------------------------ writes
    async def _write_version(self, artifact: Artifact, number: int, data: bytes, *, task_id: str | None, source_refs: list[Any],
                             note: dict[str, Any] | None, extracted_text: str | None) -> ArtifactVersion:
        key, sha = self.store.put(artifact.owner_id, data)  # bytes are durable before the row exists
        v = ArtifactVersion(artifact_id=artifact.id, version=number, storage_key=key, size=len(data), sha256=sha,
                            source_refs=list(source_refs), created_by_task_id=task_id, note=note or {}, extracted_text=extracted_text)
        self.db.add(v)
        artifact.current_version = number
        artifact.updated_at = self.clock.now()
        await self.db.flush()
        return v

    async def create(self, owner_id: str, name: str, mime: str, data: bytes, *, origin: str, task_id: str | None = None,
                     source_refs: list[Any] | None = None, note: dict[str, Any] | None = None, extracted_text: str | None = None) -> Saved:
        if len(data) > self.settings.upload_max_bytes:
            raise Invalid("files are limited to 20 MiB", code="too_large", status=413)
        a = Artifact(owner_id=owner_id, task_id=task_id, name=name, mime=mime, origin=origin, current_version=0,
                     created_at=self.clock.now(), updated_at=self.clock.now())
        self.db.add(a)
        await self.db.flush()
        v = await self._write_version(a, 1, data, task_id=task_id, source_refs=source_refs or [], note=note, extracted_text=extracted_text)
        await self._audit(owner_id, "artifact.created", a, v, actor="worker" if task_id else "owner")
        await emit(self.db, owner_id, "artifact.created", {"artifact_id": a.id, "version": 1, "name": a.name}, task_id=task_id)
        return Saved(a, v)

    async def add_version(self, owner_id: str, artifact_id: str, expected_version: int, data: bytes, *, task_id: str | None = None,
                          source_refs: list[Any] | None = None, note: dict[str, Any] | None = None, extracted_text: str | None = None) -> Saved:
        """Optimistic concurrency: a stale expected_version is a 409 and nothing is written."""
        a = await self.get(owner_id, artifact_id, for_update=True)
        if a.current_version != expected_version:
            raise Conflict(f"this file is at version {a.current_version}, not {expected_version}; reload it and apply your edit again",
                           code="version_conflict")
        if len(data) > self.settings.upload_max_bytes:
            raise Invalid("files are limited to 20 MiB", code="too_large", status=413)
        v = await self._write_version(a, expected_version + 1, data, task_id=task_id, source_refs=source_refs or [], note=note,
                                      extracted_text=extracted_text)
        await self._audit(owner_id, "artifact.version_created", a, v, actor="worker" if task_id else "owner")
        await emit(self.db, owner_id, "artifact.updated", {"artifact_id": a.id, "version": v.version}, task_id=task_id)
        await self.invalidate_attachments(a, f"{a.name} changed to version {v.version} after this email was proposed")
        return Saved(a, v)

    async def delete(self, owner_id: str, artifact_id: str) -> None:
        a = await self.get(owner_id, artifact_id, for_update=True)
        a.deleted_at = self.clock.now()
        await self.invalidate_attachments(a, f"{a.name} was deleted")
        for v in await self.versions(a):
            await self.db.delete(v)  # bytes become unreferenced; `opa artifacts-gc` removes them
        self.db.add(AuditLog(owner_id=owner_id, actor="owner", type="artifact.deleted", subject_id=a.id, sanitized={"name": a.name}))
        await emit(self.db, owner_id, "artifact.deleted", {"artifact_id": a.id})

    async def invalidate_attachments(self, artifact: Artifact, reason: str) -> int:
        """An email proposal is bound to exact attachment bytes. When the file changes or goes away,
        its open proposals are cancelled; a task waiting on one is requeued so the assistant can
        propose again with the current version (spec section 12, step 6)."""
        q = select(Action).where(Action.owner_id == artifact.owner_id, Action.state.in_(OPEN_ACTION_STATES),
                                 Action.canonical_args["attachments"].contains([{"artifact_id": artifact.id}]))
        actions = list(await self.db.scalars(q.with_for_update()))
        if not actions:
            return 0
        n = await invalidate_actions(self.db, actions, reason, self.clock)
        for action in actions:
            task = await self.db.get(Task, action.task_id)
            if task.state == "waiting_approval" and task.checkpoint.get("pending_action", {}).get("action_id") == action.id:
                await requeue(self.db, task, self.clock)
        return n

    async def _audit(self, owner_id: str, type_: str, a: Artifact, v: ArtifactVersion, *, actor: str) -> None:
        self.db.add(AuditLog(owner_id=owner_id, actor=actor, type=type_, subject_id=a.id,
                             sanitized={"version": v.version, "sha256": v.sha256, "size": v.size, "mime": a.mime}))

    # ------------------------------------------------------------ owner uploads
    async def upload(self, owner_id: str, raw_name: str, declared_type: str | None, data: bytes) -> Saved:
        try:
            checked = check_upload(raw_name, declared_type, data, self.settings.upload_max_bytes)
            extracted = await extract_text(checked.kind, data, timeout=self.settings.extraction_timeout_seconds,
                                           max_chars=self.settings.extraction_max_chars)
        except FileRejected as e:
            raise rejected(e) from None
        note: dict[str, Any] = {"original_name": (raw_name or "")[:200], "extraction": "ok", "truncated": extracted.truncated}
        if extracted.pages is not None:
            note["pages"] = extracted.pages
        return await self.create(owner_id, checked.name, checked.mime, data, origin="upload",
                                 source_refs=[{"type": "upload"}], note=note,
                                 extracted_text=extracted.text if checked.kind == "pdf" else None)

    # ------------------------------------------------------------ typed operations (tools and owner edits)
    async def create_markdown(self, owner_id: str, name: str, content: str, *, origin: str, task_id: str | None = None,
                              source_refs: list[Any] | None = None) -> Saved:
        return await self.create(owner_id, ensure_extension(name, "md"), KINDS["md"], content.encode("utf-8"), origin=origin,
                                 task_id=task_id, source_refs=source_refs)

    async def replace_markdown(self, owner_id: str, artifact_id: str, expected_version: int, content: str, *,
                               task_id: str | None = None, source_refs: list[Any] | None = None) -> Saved:
        a = await self.get(owner_id, artifact_id)
        if a.mime not in (KINDS["md"], KINDS["txt"], KINDS["csv"]):
            raise Invalid("only Markdown, text, and CSV files can be edited", code="unsupported_format")
        return await self.add_version(owner_id, artifact_id, expected_version, content.encode("utf-8"), task_id=task_id, source_refs=source_refs)

    async def edit_markdown(self, owner_id: str, artifact_id: str, expected_version: int, edits: list[dict[str, str]], *,
                            task_id: str | None = None) -> Saved:
        a = await self.get(owner_id, artifact_id)
        if a.mime != KINDS["md"]:
            raise Invalid("edit_markdown only edits Markdown files", code="unsupported_format")
        if a.current_version != expected_version:
            raise Conflict(f"this file is at version {a.current_version}, not {expected_version}; read it again and redo the edit",
                           code="version_conflict")
        text = self.bytes_of(await self.version(a)).decode("utf-8")
        if not edits:
            raise Invalid("no edits given")
        for n, e in enumerate(edits):
            old, new = e.get("old_text", ""), e.get("new_text", "")
            if not old:
                raise Invalid(f"edits[{n}].old_text must not be empty")
            count = text.count(old)
            if count != 1:
                raise Invalid(f"edits[{n}].old_text must match exactly once; it matches {count} times")
            text = text.replace(old, new, 1)
        prev = await self.version(a)
        return await self.add_version(owner_id, artifact_id, expected_version, text.encode("utf-8"), task_id=task_id, source_refs=prev.source_refs)

    async def create_csv(self, owner_id: str, name: str, rows: list[list[Any]], *, origin: str, task_id: str | None = None,
                         source_refs: list[Any] | None = None) -> tuple[Saved, int]:
        data, escaped = render.rows_to_csv(rows)
        note = {"formula_cells_escaped": escaped}
        if escaped:
            note["disclosure"] = render.CSV_DISCLOSURE.format(n=escaped)
        saved = await self.create(owner_id, ensure_extension(name, "csv"), KINDS["csv"], data, origin=origin, task_id=task_id,
                                  source_refs=source_refs, note=note)
        return saved, escaped

    async def render_pdf(self, owner_id: str, artifact_id: str, version: int | None, *, task_id: str | None = None) -> Saved:
        """Render a Markdown version to PDF. Re-rendering the same source adds a version to the
        same PDF file, so a revised brief keeps one stable PDF identity."""
        src = await self.get(owner_id, artifact_id)
        if src.mime != KINDS["md"]:
            raise Invalid("only Markdown files can be rendered to PDF", code="unsupported_format")
        sv = await self.version(src, version)
        text = self.bytes_of(sv).decode("utf-8")
        title = src.name.rsplit(".", 1)[0]
        data = render.markdown_to_pdf(text, title)
        extracted = text[: self.settings.extraction_max_chars]  # the PDF's text is its Markdown source
        ref = {"type": "artifact", "artifact_id": src.id, "version": sv.version, "sha256": sv.sha256}
        note = {"rendered_from": src.id, "rendered_from_version": sv.version, "renderer": "reportlab"}
        existing = await self.db.scalar(
            select(Artifact).join(ArtifactVersion, (ArtifactVersion.artifact_id == Artifact.id) & (ArtifactVersion.version == Artifact.current_version))
            .where(Artifact.owner_id == owner_id, Artifact.deleted_at.is_(None), Artifact.mime == KINDS["pdf"],
                   ArtifactVersion.note["rendered_from"].astext == src.id)
            .order_by(Artifact.created_at.desc()).limit(1))
        if existing is not None:
            return await self.add_version(owner_id, existing.id, existing.current_version, data, task_id=task_id, source_refs=[ref],
                                          note=note, extracted_text=extracted)
        return await self.create(owner_id, ensure_extension(title, "pdf"), KINDS["pdf"], data, origin="generated" if task_id else "owner",
                                 task_id=task_id, source_refs=[ref], note=note, extracted_text=extracted)


async def gc_orphans(sm: async_sessionmaker[AsyncSession], settings: Settings, older_than: timedelta = timedelta(hours=1)) -> int:
    """Delete stored files that no version row references (failed transactions, deleted files)."""
    store = ArtifactStore(settings.artifact_dir)
    candidates = list(store.iter_keys(older_than.total_seconds()))
    if not candidates:
        return 0
    async with sm() as db:
        keys = [k for k, _ in candidates if k]
        live = set(await db.scalars(select(ArtifactVersion.storage_key).where(ArtifactVersion.storage_key.in_(keys)))) if keys else set()
    removed = 0
    for key, path in candidates:
        if key is None or key not in live:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
