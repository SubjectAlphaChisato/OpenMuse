"""Bounded text extraction (ART-01): PDFs are parsed in a separate process with a wall-clock
timeout, a CPU limit, and an output cap; text formats are decoded in-process with the same cap."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from opa.artifacts.files import FileRejected, decode_text


@dataclass(frozen=True)
class Extracted:
    text: str
    truncated: bool
    pages: int | None = None


async def run_pdf_extraction(data: bytes, *, timeout: float, max_chars: int, cmd: Sequence[str] | None = None) -> Extracted:
    """Raise FileRejected('extraction_timeout' | 'unsupported_format' | 'invalid_file') on failure.
    `cmd` replaces the child command (tests use it to simulate a hanging parser)."""
    cpu = max(1, int(timeout) + 1)  # the child applies this CPU rlimit to itself (no preexec_fn in a threaded server)
    argv = list(cmd) if cmd else [sys.executable, "-m", "opa.artifacts.extract_worker", str(max_chars), str(cpu)]
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join(p for p in sys.path if p), "LANG": "C.UTF-8"}
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input=data), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise FileRejected("extraction_timeout", f"text extraction took longer than {timeout:g} s and was stopped") from None
    if proc.returncode != 0:
        raise FileRejected("invalid_file", "the PDF could not be processed")
    if len(out) > max_chars * 6 + 4096:  # JSON-escaped text can grow; anything beyond this is a misbehaving child
        raise FileRejected("invalid_file", "extraction produced too much output")
    try:
        res = json.loads(out)
    except ValueError:
        raise FileRejected("invalid_file", "the PDF could not be processed") from None
    if res.get("status") != "ok":
        raise FileRejected(res.get("status") if res.get("status") in ("unsupported_format", "invalid_file") else "invalid_file",
                           res.get("detail", "the PDF could not be processed"))
    text = str(res.get("text", ""))[:max_chars]
    return Extracted(text=text, truncated=bool(res.get("truncated")), pages=res.get("pages"))


async def extract_text(kind: str, data: bytes, *, timeout: float, max_chars: int) -> Extracted:
    if kind == "pdf":
        return await run_pdf_extraction(data, timeout=timeout, max_chars=max_chars)
    text, truncated = decode_text(data, max_chars)
    return Extracted(text=text, truncated=truncated)
