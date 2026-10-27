"""Share a conversation as a read-only snapshot anyone with the link can open.

The owner picks the messages; the snapshot is frozen at that moment, so later messages never
appear. Only the owner's messages and the assistant's replies are included: account cards,
system notes, and files are not. The link token is random (256 bits), stored only as a SHA-256,
and shown once. The public page is static HTML with no scripts, cookies, or outside requests,
kept out of search engines, and every unknown, revoked, or expired link gets the same 404.
"""
from __future__ import annotations

import html
import secrets
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from opa.api.deps import ClockDep, DbDep, OwnerDep, SettingsDep, get_owned
from opa.core.canonical import sha256_hex
from opa.core.errors import Invalid
from opa.db.models import AuditLog, Conversation, Message, User, Share
from opa.db.outbox import emit

router = APIRouter(prefix="/api", tags=["shares"])
public = APIRouter(include_in_schema=False)

MAX_MESSAGES = 200


class ShareIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    message_ids: list[str] | None = Field(default=None, max_length=MAX_MESSAGES)  # null: every shareable message
    expires_in_days: Literal[1, 7, 30] = 7


class ShareOut(BaseModel):
    id: str
    conversation_id: str
    title: str
    message_count: int
    status: Literal["active", "expired", "revoked"]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    view_count: int
    last_viewed_at: datetime | None


class ShareCreated(BaseModel):
    share: ShareOut
    url: str  # shown once; only a hash of the token is stored


def _status(s: Share, now: datetime) -> str:
    return "revoked" if s.revoked_at else "expired" if s.expires_at <= now else "active"


def out(s: Share, now: datetime) -> ShareOut:
    return ShareOut(id=s.id, conversation_id=s.conversation_id, title=s.title, message_count=s.message_count, status=_status(s, now),
                    created_at=s.created_at, expires_at=s.expires_at, revoked_at=s.revoked_at, view_count=s.view_count,
                    last_viewed_at=s.last_viewed_at)


def shareable(m: Message) -> bool:
    """Only conversation text. Account cards describe the owner's accounts, and system notes are internal."""
    return m.role in ("user", "assistant") and not m.connection_request_id and bool(m.content.strip())


@router.post("/conversations/{conversation_id}/shares", response_model=ShareCreated, status_code=201)
async def create_share(conversation_id: str, body: ShareIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep) -> ShareCreated:
    conv = await get_owned(db, Conversation, conversation_id, owner)
    rows = list(await db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)))
    if body.message_ids is not None:
        wanted = set(body.message_ids)
        unknown = wanted - {m.id for m in rows}
        if unknown:
            raise Invalid("some messages are not in this conversation", code="unknown_message")
        rows = [m for m in rows if m.id in wanted]
    picked = [m for m in rows if shareable(m)][:MAX_MESSAGES]
    if not picked:
        raise Invalid("there is nothing to share in the chosen messages", code="nothing_to_share")
    snapshot = [{"role": m.role, "text": m.content, "at": m.created_at.isoformat(), "files_not_shared": len(m.artifact_ids)} for m in picked]
    token = secrets.token_urlsafe(32)
    now = clock.now()
    title = (body.title or "").strip() or conv.title
    share = Share(owner_id=owner.id, conversation_id=conv.id, token_hash=sha256_hex(token), title=title[:200], snapshot=snapshot,
                  message_count=len(snapshot), expires_at=now + timedelta(days=body.expires_in_days))
    db.add(share)
    await db.flush()
    await db.refresh(share)
    db.add(AuditLog(owner_id=owner.id, actor="owner", type="share.created", subject_id=share.id,
                    sanitized={"conversation_id": conv.id, "messages": len(snapshot), "expires_in_days": body.expires_in_days}))
    await emit(db, owner.id, "share.created", {"share_id": share.id})
    return ShareCreated(share=out(share, now), url=f"{settings.public_base_url.rstrip('/')}/s/{token}")


@router.get("/shares", response_model=list[ShareOut])
async def list_shares(db: DbDep, owner: OwnerDep, clock: ClockDep) -> list[ShareOut]:
    rows = await db.scalars(select(Share).where(Share.owner_id == owner.id).order_by(Share.created_at.desc()))
    return [out(s, clock.now()) for s in rows]


@router.delete("/shares/{share_id}", response_model=ShareOut)
async def revoke_share(share_id: str, db: DbDep, owner: OwnerDep, clock: ClockDep) -> ShareOut:
    share = await get_owned(db, Share, share_id, owner)
    if share.revoked_at is None:
        share.revoked_at = clock.now()
        share.snapshot = []  # nothing left to serve
        db.add(AuditLog(owner_id=owner.id, actor="owner", type="share.revoked", subject_id=share.id, sanitized={}))
        await emit(db, owner.id, "share.revoked", {"share_id": share.id})
    return out(share, clock.now())


async def clear_expired(sm, now: datetime) -> int:
    """Hourly (scheduler): expired snapshots are deleted, keeping only the record that a share existed."""
    async with sm() as db, db.begin():
        res = await db.execute(update(Share).where(Share.expires_at <= now, func.jsonb_array_length(Share.snapshot) > 0).values(snapshot=[]))
        return res.rowcount or 0


# ------------------------------------------------------------------ the public page

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
STYLE = """
body{margin:0;background:#f6f6f4;color:#1a1a1a;font:16px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 48px}
header{margin-bottom:24px}h1{font-size:24px;margin:0 0 4px}.meta{color:#666;font-size:14px}
ol{list-style:none;padding:0;display:grid;gap:10px;margin:0}
li{padding:10px 14px;border-radius:12px;max-width:85%;white-space:pre-wrap;overflow-wrap:anywhere}
li.user{background:#1a1a1a;color:#fff;justify-self:end}li.assistant{background:#fff;border:1px solid #e4e4de}
.who{display:block;font-size:12px;opacity:.7;margin-bottom:2px}.note{font-size:12px;opacity:.7;display:block;margin-top:4px}
footer{margin-top:32px;color:#666;font-size:13px;border-top:1px solid #e4e4de;padding-top:12px}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta name="robots" content="noindex,nofollow"><title>{html.escape(title)}</title><style>{STYLE}</style></head>'
           f"<body><main>{body}</main></body></html>")
    return HTMLResponse(doc, status_code=status, headers=HEADERS)


def _not_found() -> HTMLResponse:
    return _page("Link not available", "<h1>This link is not available</h1><p class=\"meta\">It may have expired or been turned off by the person who shared it.</p>", 404)


def _date(d: datetime) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def render(share: Share, assistant: str) -> HTMLResponse:
    e = html.escape
    items = []
    for m in share.snapshot:
        who = "You" if m["role"] == "user" else assistant
        note = f'<span class="note">{m["files_not_shared"]} attached file(s) not shared</span>' if m.get("files_not_shared") else ""
        items.append(f'<li class="{"user" if m["role"] == "user" else "assistant"}"><span class="who">{e(who)}</span>{e(m["text"])}{note}</li>')
    shared = _date(share.created_at)
    body = (f"<header><h1>{e(share.title)}</h1><p class=\"meta\">A conversation with {e(assistant)}, shared {e(shared)}. Read only.</p></header>"
            f"<ol>{''.join(items)}</ol>"
            f"<footer>This is a snapshot: later messages are not included. The link stops working on "
            f"{e(_date(share.expires_at))} or when the person who shared it turns it off.</footer>")
    return _page(share.title, body)


@public.get("/s/{token}")
async def view_share(token: str, request: Request) -> HTMLResponse:
    if not (20 <= len(token) <= 100):
        return _not_found()
    clock = request.app.state.clock
    now = clock.now()
    async with request.app.state.sessionmaker() as db, db.begin():
        share = await db.scalar(select(Share).where(Share.token_hash == sha256_hex(token)).with_for_update())
        if share is None or share.revoked_at is not None or share.expires_at <= now or not share.snapshot:
            return _not_found()
        share.view_count += 1
        share.last_viewed_at = now
        owner = await db.get(User, share.owner_id)
        return render(share, owner.assistant_name or "Assistant")
