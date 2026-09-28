"""Filename sanitization and content sniffing for uploads (ART-01).

python-magic needs the libmagic system library; the four accepted formats are identified
reliably by their bytes, so a small fixed sniffer is used instead (see tech-design section 19).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

KINDS = {
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "pdf": "application/pdf",
}
EXTENSIONS = {".txt": "txt", ".text": "txt", ".md": "md", ".markdown": "md", ".csv": "csv", ".pdf": "pdf"}
DECLARED_TYPES = {
    "text/plain": {"txt", "md", "csv"},  # browsers often send text/plain for .md and .csv
    "text/markdown": {"md"},
    "text/x-markdown": {"md"},
    "text/csv": {"csv"},
    "application/csv": {"csv"},
    "application/vnd.ms-excel": {"csv"},  # Windows browsers label .csv this way
    "application/pdf": {"pdf"},
}
GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream"}

_BINARY_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"PK\x03\x04", "ZIP archive (or DOCX/XLSX)"),
    (b"\x1f\x8b", "gzip archive"),
    (b"\x7fELF", "executable"),
    (b"MZ", "Windows executable"),
    (b"\xcf\xfa\xed\xfe", "executable"),
    (b"\xca\xfe\xba\xbe", "executable"),
    (b"Rar!", "RAR archive"),
    (b"7z\xbc\xaf", "7z archive"),
]

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class FileRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sanitize_filename(raw: str, default: str = "file") -> str:
    """Reduce any client-supplied name to [A-Za-z0-9._-]. Directory parts are dropped, so the
    result can never contain a separator or be '.'/'..'. It is metadata only; storage uses the hash."""
    name = unicodedata.normalize("NFKD", raw or "")
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "")
    name = _SAFE.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("._-")
    if not name:
        name = default
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 120 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else name[:120]
    return name


def kind_from_name(name: str) -> str | None:
    lower = name.lower()
    for ext, kind in EXTENSIONS.items():
        if lower.endswith(ext):
            return kind
    return None


def sniff(data: bytes) -> tuple[str, str]:
    """Classify bytes as ('pdf'|'text'|'binary', description)."""
    head = data[:1024]
    if b"%PDF-" in head:
        return "pdf", "PDF document"
    for magic, desc in _BINARY_MAGIC:
        if data.startswith(magic):
            return "binary", desc
    if b"\x00" in data:
        return "binary", "binary data"
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "binary", "binary or non-UTF-8 data"
    return "text", "UTF-8 text"


@dataclass(frozen=True)
class Checked:
    name: str
    kind: str  # txt | md | csv | pdf
    mime: str


def check_upload(raw_name: str, declared_type: str | None, data: bytes, max_bytes: int) -> Checked:
    """Size, type, and name checks. Raises FileRejected with a stable error code."""
    if len(data) > max_bytes:
        raise FileRejected("too_large", f"files are limited to {max_bytes // (1024 * 1024)} MiB")
    if not data:
        raise FileRejected("invalid_file", "the file is empty")
    name = sanitize_filename(raw_name)
    kind = kind_from_name(name)
    if kind is None:
        raise FileRejected("unsupported_format", "only .txt, .md, .csv, and text-based .pdf files are supported")
    declared = (declared_type or "").split(";")[0].strip().lower()
    if declared not in GENERIC_TYPES and kind not in DECLARED_TYPES.get(declared, set()):
        raise FileRejected("type_mismatch", f"the declared type {declared} does not match a .{kind} file")
    actual, desc = sniff(data)
    expected = "pdf" if kind == "pdf" else "text"
    if actual != expected:
        raise FileRejected("type_mismatch", f"the file is named .{kind} but its content is {desc}")
    return Checked(name=name, kind=kind, mime=KINDS[kind])


def decode_text(data: bytes, max_chars: int) -> tuple[str, bool]:
    text = data.decode("utf-8-sig", errors="replace")
    return text[:max_chars], len(text) > max_chars
