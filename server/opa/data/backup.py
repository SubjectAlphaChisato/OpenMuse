"""DATA-01 backup and restore (operator commands).

backup  : pg_dump of schema public (custom format) + a tar of the artifact store + manifest with SHA-256s.
          With --broker, schema broker (encrypted provider credentials) goes to a separate file; it is
          useless without OPA_BROKER_KEK, which must be backed up separately and never with these files.
restore : verifies the manifest, refuses a non-empty target, restores, then makes the result safe to
          start: automations paused, pending approvals invalidated, in-flight writes marked unknown,
          leases cleared, browser sessions closed. Run `opa db-roles` afterwards to re-grant roles.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

FORMAT = 1


class BackupError(Exception):
    pass


def libpq_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")


def pg_tool(name: str, bin_dir: str | None = None) -> str:
    for cand in ([str(Path(bin_dir) / name)] if bin_dir else []) + [shutil.which(name) or ""]:
        if cand and Path(cand).is_file():
            return cand
    raise BackupError(f"{name} was not found; install the PostgreSQL client tools or set OPA_PG_BIN_DIR")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise BackupError(f"{Path(cmd[0]).name} failed: {r.stderr.strip()[-800:]}")


def backup(database_url: str, artifact_dir: str, out_dir: str, *, include_broker: bool = False, bin_dir: str | None = None) -> dict[str, Any]:
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise BackupError(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    url = libpq_url(database_url)
    dump = pg_tool("pg_dump", bin_dir)
    # One consistent snapshot per dump; the app schema and the broker schema are separate files.
    _run([dump, "--format=custom", "--schema=public", "--no-owner", "--no-privileges", "--file", str(out / "app.dump"), "--dbname", url])
    if include_broker:
        _run([dump, "--format=custom", "--schema=broker", "--no-owner", "--no-privileges", "--file", str(out / "broker.dump"), "--dbname", url])
    art = Path(artifact_dir)
    with tarfile.open(out / "artifacts.tar.gz", "w:gz") as tar:
        if art.is_dir():
            tar.add(art, arcname="artifacts")
    with psycopg.connect(url) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    files = {p.name: {"size": p.stat().st_size, "sha256": _sha(p)} for p in sorted(out.iterdir())}
    manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(), "alembic_revision": revision,
                "includes_broker": include_broker, "files": files,
                "note": "broker.dump (if present) needs OPA_BROKER_KEK, which is not in this backup."}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _verify(src: Path) -> dict[str, Any]:
    manifest_path = src / "manifest.json"
    if not manifest_path.is_file():
        raise BackupError("manifest.json is missing")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unsupported backup format {manifest.get('format')}")
    for name, meta in manifest["files"].items():
        p = src / name
        if not p.is_file() or _sha(p) != meta["sha256"]:
            raise BackupError(f"{name} is missing or does not match the manifest")
    return manifest


def restore(database_url: str, artifact_dir: str, src_dir: str, *, bin_dir: str | None = None) -> dict[str, Any]:
    src = Path(src_dir)
    manifest = _verify(src)
    url = libpq_url(database_url)
    with psycopg.connect(url) as conn:
        n = conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('public', 'broker')").fetchone()[0]
    if n:
        raise BackupError("the target database is not empty; restore into a fresh database")
    art = Path(artifact_dir)
    if art.exists() and any(art.iterdir()):
        raise BackupError(f"the artifact directory {art} is not empty")
    restore_tool = pg_tool("pg_restore", bin_dir)
    # public already exists in a fresh database, so skip its CREATE SCHEMA entry
    listing = subprocess.run([restore_tool, "--list", str(src / "app.dump")], capture_output=True, text=True, check=True).stdout
    keep = "\n".join(line for line in listing.splitlines() if " SCHEMA - public " not in line)
    toc = src / ".restore-toc"
    toc.write_text(keep)
    try:
        _run([restore_tool, "--no-owner", "--no-privileges", "--exit-on-error", "--use-list", str(toc), "--dbname", url, str(src / "app.dump")])
    finally:
        toc.unlink(missing_ok=True)
    if manifest.get("includes_broker") and (src / "broker.dump").is_file():
        _run([restore_tool, "--no-owner", "--no-privileges", "--exit-on-error", "--dbname", url, str(src / "broker.dump")])
    art.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src / "artifacts.tar.gz", "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name == "artifacts" or m.name.startswith("artifacts/")]
        for m in members:
            m.name = m.name[len("artifacts/"):] if m.name.startswith("artifacts/") else "."
        tar.extractall(art, members=members, filter="data")  # the "data" filter refuses links and paths outside the target
    summary = make_safe(url)
    return {"manifest": manifest, "safety": summary}


def make_safe(url: str) -> dict[str, int]:
    """Post-restore rules from the spec: nothing resumes on its own and nothing is re-sent."""
    reason = "restored from backup; propose again if still needed"
    with psycopg.connect(url) as conn, conn.transaction():
        out = {}
        out["automations_paused"] = conn.execute(
            "UPDATE automations SET paused_reason = 'restored' WHERE deleted_at IS NULL AND enabled").rowcount
        out["approvals_invalidated"] = conn.execute(
            "UPDATE approvals SET state = 'invalidated', reason = %s, decided_at = now() WHERE state IN ('pending', 'approved')", (reason,)).rowcount
        out["actions_cancelled"] = conn.execute(
            "UPDATE actions SET state = 'cancelled', error = %s WHERE state IN ('awaiting_approval', 'approved')",
            (json.dumps({"code": "approval_required", "message": reason}),)).rowcount
        out["actions_unknown"] = conn.execute(
            "UPDATE actions SET state = 'unknown', error = %s WHERE state = 'dispatching'",
            (json.dumps({"code": "unknown_outcome", "message": "the backup was taken while this was being sent; check whether it happened"}),)).rowcount
        out["tasks_blocked"] = conn.execute(
            "UPDATE tasks SET state = 'blocked', blocker_code = 'restored', blocker_detail = %s, version = version + 1 WHERE state IN ('waiting_approval', 'running')",
            ("restored from a backup; review it and resume",)).rowcount
        conn.execute("UPDATE jobs SET lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL")
        out["browser_sessions_closed"] = conn.execute(
            "UPDATE browser_sessions SET state = 'stopped', ended_at = now() WHERE state = 'active'").rowcount
        out["shares_revoked"] = conn.execute(  # a link revoked after the backup must not come back
            "UPDATE shares SET revoked_at = now(), snapshot = '[]'::jsonb WHERE revoked_at IS NULL").rowcount
        out["connection_requests_expired"] = conn.execute(  # v0.2: an account card from before the backup cannot be confirmed
            "UPDATE connection_requests SET status = 'expired', decided_at = now() WHERE status IN ('pending', 'waiting_for_provider')").rowcount
        for (owner_id,) in conn.execute("SELECT id FROM owners").fetchall():
            conn.execute("INSERT INTO notifications (id, owner_id, type, title, body, created_at) VALUES (%s, %s, 'system.restored', %s, %s, now())",
                         (_ulid(), owner_id, "Restored from a backup",
                          "Automations are paused, pending approvals were cancelled, and shared links were turned off. Review them, then resume what you still want."))
    return out


def _ulid() -> str:
    from opa.core.ids import new_id

    return new_id()

