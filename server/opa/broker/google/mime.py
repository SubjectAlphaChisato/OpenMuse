"""Gmail message parsing and RFC 2822 construction."""
from __future__ import annotations

import base64
import html
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

BODY_LIMIT = 50_000


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def headers_of(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1
        if tag == "a" and self.href:
            self.out.append(f" ({self.href})")
            self.href = None
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(markup: str) -> str:
    p = _Text()
    p.feed(markup)
    lines = [" ".join(line.split()) for line in "".join(p.out).splitlines()]
    text, blank = [], 0
    for line in lines:
        blank = blank + 1 if not line else 0
        if blank <= 1:
            text.append(line)
    return "\n".join(text).strip()


def _charset(part: dict[str, Any]) -> str:
    ctype = headers_of(part).get("content-type", "")
    for piece in ctype.split(";")[1:]:
        k, _, v = piece.strip().partition("=")
        if k.lower() == "charset":
            return v.strip('"') or "utf-8"
    return "utf-8"


def _decode(part: dict[str, Any]) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = b64url_decode(data)
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)


def extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to text/html converted to text (links kept). Attachments are skipped."""
    plain = html_part = None
    for p in _walk(payload):
        if p.get("filename"):
            continue
        mt = p.get("mimeType", "")
        if mt == "text/plain" and plain is None:
            plain = _decode(p)
        elif mt == "text/html" and html_part is None:
            html_part = _decode(p)
    text = plain if plain else (html_to_text(html_part) if html_part else "")
    return text[:BODY_LIMIT]


def normalize_message(m: dict[str, Any], *, with_body: bool) -> dict[str, Any]:
    h = headers_of(m.get("payload", {}))
    out = {
        "id": m["id"], "thread_id": m.get("threadId"), "from": h.get("from", ""), "to": h.get("to", ""), "cc": h.get("cc", ""),
        "subject": h.get("subject", ""), "date": h.get("date", ""), "snippet": html.unescape(m.get("snippet", "")),
        "labels": m.get("labelIds", []),
    }
    if with_body:
        out["body"] = extract_body(m.get("payload", {}))
    return out


def build_raw(c: dict[str, Any], now: datetime, *, in_reply_to: str | None = None, references: str | None = None) -> str:
    msg = EmailMessage()
    msg["From"] = c["from"]
    msg["To"] = ", ".join(c["to"])
    if c["cc"]:
        msg["Cc"] = ", ".join(c["cc"])
    if c["bcc"]:
        msg["Bcc"] = ", ".join(c["bcc"])  # Gmail delivers to Bcc recipients and strips the header
    msg["Subject"] = c["subject"]
    msg["Message-ID"] = c["message_id_header"]
    msg["Date"] = format_datetime(now)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(c["body"])
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
