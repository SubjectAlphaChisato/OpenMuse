"""Files and artifacts (ART-01). User-authorized only; no public links in v0.1.

Uploads are the raw request body (the file name is a query parameter), which keeps the size
limit enforceable while streaming and needs no multipart parser.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from opa.api.deps import ClockDep, DbDep, OwnerDep, SettingsDep
from opa.artifacts import render
from opa.artifacts.files import KINDS
from opa.artifacts.service import ArtifactService
from opa.core.errors import Invalid
from opa.db.models import Artifact, ArtifactVersion

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


class VersionOut(BaseModel):
    version: int
    size: int
    sha256: str
    source_refs: list[Any]
    created_by_task_id: str | None
    note: dict[str, Any]
    created_at: datetime


class ArtifactOut(BaseModel):
    id: str
    name: str
    mime: str
    origin: str
    task_id: str | None
    current_version: int
    created_at: datetime
    updated_at: datetime
    current: VersionOut | None = None


class MarkdownIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=1_000_000)


class EditIn(BaseModel):
    expected_version: int
    content: str = Field(max_length=5_000_000)


class RenderIn(BaseModel):
    version: int | None = None


class PreviewOut(BaseModel):
    kind: Literal["markdown", "text", "csv", "pdf"]
    version: int
    html: str | None = None  # sanitized server-side; raw HTML in the source is shown as text
    text: str | None = None
    rows: list[list[str]] | None = None
    truncated: bool = False
    note: dict[str, Any] = {}


def _vout(v: ArtifactVersion) -> VersionOut:
    return VersionOut(version=v.version, size=v.size, sha256=v.sha256, source_refs=v.source_refs, created_by_task_id=v.created_by_task_id,
                      note=v.note, created_at=v.created_at)


async def _out(svc: ArtifactService, a: Artifact) -> ArtifactOut:
    return ArtifactOut(id=a.id, name=a.name, mime=a.mime, origin=a.origin, task_id=a.task_id, current_version=a.current_version,
                       created_at=a.created_at, updated_at=a.updated_at, current=_vout(await svc.version(a)))


@router.get("", response_model=list[ArtifactOut])
async def list_artifacts(db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep, limit: int = 200) -> list[ArtifactOut]:
    svc = ArtifactService(db, settings, clock)
    return [await _out(svc, a) for a in await svc.list(owner.id, max(1, min(limit, 500)))]


@router.post("", response_model=ArtifactOut, status_code=201)
async def upload(request: Request, filename: str, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> ArtifactOut:
    """Body: the file bytes. Rejected with 413 over 20 MiB, 422 `type_mismatch`, `unsupported_format`
    (including scanned PDFs), `extraction_timeout`, or `invalid_file`."""
    declared_len = request.headers.get("content-length")
    if declared_len and declared_len.isdigit() and int(declared_len) > settings.upload_max_bytes:
        raise Invalid("files are limited to 20 MiB", code="too_large", status=413)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > settings.upload_max_bytes:
            raise Invalid("files are limited to 20 MiB", code="too_large", status=413)
        chunks.append(chunk)
    svc = ArtifactService(db, settings, clock)
    saved = await svc.upload(owner.id, filename, request.headers.get("content-type"), b"".join(chunks))
    return await _out(svc, saved.artifact)


@router.post("/markdown", response_model=ArtifactOut, status_code=201)
async def create_markdown(body: MarkdownIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> ArtifactOut:
    svc = ArtifactService(db, settings, clock)
    saved = await svc.create_markdown(owner.id, body.name, body.content, origin="owner", source_refs=[{"type": "owner"}])
    return await _out(svc, saved.artifact)


@router.get("/{artifact_id}", response_model=ArtifactOut)
async def get_artifact(artifact_id: str, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> ArtifactOut:
    svc = ArtifactService(db, settings, clock)
    return await _out(svc, await svc.get(owner.id, artifact_id))


@router.patch("/{artifact_id}", response_model=ArtifactOut)
async def edit_artifact(artifact_id: str, body: EditIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> ArtifactOut:
    """Save new text as the next version. A stale expected_version is 409 `version_conflict`.
    Pending email approvals that attach this file are cancelled (their bytes changed)."""
    svc = ArtifactService(db, settings, clock)
    saved = await svc.replace_markdown(owner.id, artifact_id, body.expected_version, body.content, source_refs=[{"type": "owner_edit"}])
    return await _out(svc, saved.artifact)


@router.delete("/{artifact_id}", status_code=204)
async def delete_artifact(artifact_id: str, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> Response:
    await ArtifactService(db, settings, clock).delete(owner.id, artifact_id)
    return Response(status_code=204)


@router.get("/{artifact_id}/versions", response_model=list[VersionOut])
async def list_versions(artifact_id: str, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> list[VersionOut]:
    svc = ArtifactService(db, settings, clock)
    return [_vout(v) for v in await svc.versions(await svc.get(owner.id, artifact_id))]


@router.get("/{artifact_id}/versions/{version}/download")
async def download(artifact_id: str, version: int, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> Response:
    svc = ArtifactService(db, settings, clock)
    a = await svc.get(owner.id, artifact_id)
    v = await svc.version(a, version)
    data = svc.bytes_of(v)
    stem, dot, ext = a.name.rpartition(".")
    fname = f"{stem}-v{v.version}.{ext}" if dot else f"{a.name}-v{v.version}"
    return Response(content=data, media_type=a.mime if a.mime == KINDS["pdf"] else f"{a.mime}; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"{fname}\"; filename*=UTF-8''{quote(fname)}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "private, no-store",
        "ETag": f"\"{v.sha256}\"",
    })


@router.get("/{artifact_id}/versions/{version}/preview", response_model=PreviewOut)
async def preview(artifact_id: str, version: int, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> PreviewOut:
    svc = ArtifactService(db, settings, clock)
    a = await svc.get(owner.id, artifact_id)
    v = await svc.version(a, version)
    text, truncated = svc.text_of(a, v, limit=500_000)
    if a.mime == KINDS["md"]:
        return PreviewOut(kind="markdown", version=v.version, html=render.markdown_to_safe_html(text), truncated=truncated, note=v.note)
    if a.mime == KINDS["csv"]:
        return PreviewOut(kind="csv", version=v.version, rows=render.csv_preview(text), truncated=truncated, note=v.note)
    return PreviewOut(kind="pdf" if a.mime == KINDS["pdf"] else "text", version=v.version, text=text, truncated=truncated, note=v.note)


@router.post("/{artifact_id}/render-pdf", response_model=ArtifactOut, status_code=201)
async def render_pdf(artifact_id: str, body: RenderIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> ArtifactOut:
    svc = ArtifactService(db, settings, clock)
    saved = await svc.render_pdf(owner.id, artifact_id, body.version)
    return await _out(svc, saved.artifact)
