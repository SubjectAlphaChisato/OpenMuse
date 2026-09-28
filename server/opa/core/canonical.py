"""Canonical JSON and hashing used for action payloads and dedup keys."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def args_hash(tool_name: str, tool_version: str, arguments: dict[str, Any]) -> str:
    return sha256_hex(f"{tool_name}@{tool_version}\n{canonical_json(arguments)}")
