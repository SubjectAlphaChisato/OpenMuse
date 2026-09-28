"""Attachment binding for email actions (APP-01, A02; design section 6.1).

At proposal time an attachment reference {artifact_id, version} is replaced by the exact
version's name, type, size, and SHA-256, so the approval hash covers the bytes. At dispatch the
broker checks again that the file still exists, that the approved version is still the current
one, and that the stored bytes still hash to the approved digest. Any difference cancels the
action; a new approval is required.

Reads use only the artifact columns granted to the broker role (db/roles.py).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.artifacts.storage import ArtifactStore, StorageError, sha256_bytes
from opa.core.settings import Settings
from opa.db.models import Artifact, ArtifactVersion
from opa.sdk.contract import ConnectorError

MAX_ATTACHMENTS = 10
MAX_TOTAL_BYTES = 20 * 1024 * 1024
SUPPORTED_PROVIDERS = ("fixture",)  # the Gmail adapter gains MIME attachments when M3 merges


async def _load(db: AsyncSession, owner_id: str, artifact_id: str, version: int):
    art = (await db.execute(select(Artifact.id, Artifact.owner_id, Artifact.name, Artifact.mime, Artifact.current_version, Artifact.deleted_at)
                            .where(Artifact.id == artifact_id))).first()
    if art is None or art.owner_id != owner_id or art.deleted_at is not None:
        return None, None
    ver = (await db.execute(select(ArtifactVersion.version, ArtifactVersion.storage_key, ArtifactVersion.size, ArtifactVersion.sha256)
                            .where(ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version == version))).first()
    return art, ver


async def resolve(db: AsyncSession, owner_id: str, refs: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    if not refs:
        return []
    if provider not in SUPPORTED_PROVIDERS:
        raise ConnectorError("invalid_input", "email attachments are not available for this account type yet; send without attachments or link to the file")
    if len(refs) > MAX_ATTACHMENTS:
        raise ConnectorError("invalid_input", f"at most {MAX_ATTACHMENTS} attachments")
    out: list[dict[str, Any]] = []
    total = 0
    for n, ref in enumerate(refs):
        art, ver = await _load(db, owner_id, str(ref.get("artifact_id", "")), int(ref.get("version") or 0))
        if art is None:
            raise ConnectorError("not_found", f"attachments[{n}]: file not found")
        if ver is None:
            raise ConnectorError("not_found", f"attachments[{n}]: {art.name} has no version {ref.get('version')}")
        if ver.version != art.current_version:
            raise ConnectorError("conflict", f"attachments[{n}]: {art.name} is now at version {art.current_version}; attach the current version")
        if any(o["artifact_id"] == art.id for o in out):
            raise ConnectorError("invalid_input", f"attachments[{n}]: {art.name} is attached twice")
        total += ver.size
        out.append({"artifact_id": art.id, "version": ver.version, "name": art.name, "mime": art.mime, "size": ver.size, "sha256": ver.sha256})
    if total > MAX_TOTAL_BYTES:
        raise ConnectorError("invalid_input", "attachments exceed 20 MiB in total")
    return out


async def verify(db: AsyncSession, settings: Settings, owner_id: str, attachments: list[dict[str, Any]]) -> str | None:
    """Return None if every approved attachment is unchanged, else the reason dispatch must stop."""
    store = ArtifactStore(settings.artifact_dir)
    for a in attachments:
        art, ver = await _load(db, owner_id, a["artifact_id"], a["version"])
        if art is None:
            return f"the attachment {a['name']} was deleted after approval"
        if ver is None or ver.version != art.current_version:
            return f"the attachment {a['name']} changed after approval (now version {art.current_version})"
        if ver.sha256 != a["sha256"] or ver.size != a["size"]:
            return f"the attachment {a['name']} does not match the approved bytes"
        try:
            data = store.read(ver.storage_key)
        except (StorageError, OSError):
            return f"the attachment {a['name']} could not be read"
        if sha256_bytes(data) != a["sha256"]:
            return f"the stored bytes of {a['name']} do not match the approved attachment"
    return None
