"""Content-addressed artifact storage (ART-01, design section 11).

On-disk path: {root}/{owner_id}/{sha256[:2]}/{sha256}. Filenames are metadata only, so a
hostile name can never become a path. Writes go to a temp file in the target directory, are
fsynced, then atomically renamed; the metadata row is inserted afterwards by the caller.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

OWNER_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")  # ULID
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_RE = re.compile(r"^([0-9A-HJKMNP-TV-Z]{26})/([0-9a-f]{2})/([0-9a-f]{64})$")


class StorageError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()

    def key_for(self, owner_id: str, sha: str) -> str:
        if not OWNER_RE.match(owner_id) or not SHA_RE.match(sha):
            raise StorageError("invalid owner or digest")
        return f"{owner_id}/{sha[:2]}/{sha}"

    def path_of(self, key: str) -> Path:
        """Resolve a storage key. Only keys of the exact generated shape are accepted, and the
        resolved path must stay under the root even if a directory was replaced by a symlink."""
        if not KEY_RE.match(key):
            raise StorageError("invalid storage key")
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root):
            raise StorageError("storage key escapes the artifact directory")
        return p

    def put(self, owner_id: str, data: bytes) -> tuple[str, str]:
        """Store bytes; returns (storage_key, sha256). Identical bytes share one file."""
        sha = sha256_bytes(data)
        key = self.key_for(owner_id, sha)
        final = self.path_of(key)
        if final.exists() and sha256_bytes(final.read_bytes()) == sha:
            os.utime(final)  # keeps a just-reused file out of the orphan sweep window
            return key, sha
        final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, final)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return key, sha

    def read(self, key: str) -> bytes:
        return self.path_of(key).read_bytes()

    def read_verified(self, key: str, sha: str) -> bytes:
        data = self.read(key)
        if sha256_bytes(data) != sha:
            raise StorageError("stored bytes do not match their recorded hash")
        return data

    def delete(self, key: str) -> None:
        try:
            self.path_of(key).unlink()
        except FileNotFoundError:
            pass

    def iter_keys(self, older_than_seconds: float = 0):
        """Yield (key, path) for stored files (and stale temp files as key None)."""
        if not self.root.exists():
            return
        cutoff = time.time() - older_than_seconds
        for owner_dir in self.root.iterdir():
            if not owner_dir.is_dir() or not OWNER_RE.match(owner_dir.name):
                continue
            for prefix in owner_dir.iterdir():
                if not prefix.is_dir():
                    continue
                for f in prefix.iterdir():
                    if f.stat().st_mtime > cutoff:
                        continue
                    if f.name.startswith(".tmp-"):
                        yield None, f
                    elif SHA_RE.match(f.name):
                        yield f"{owner_dir.name}/{prefix.name}/{f.name}", f
