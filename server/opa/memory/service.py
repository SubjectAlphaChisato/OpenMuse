"""Memory records and the Markdown editor view (MEM-01).

The editor shows confirmed items grouped by scope, one list item per record, with the record
ID in a hidden HTML comment. Saving diffs the document against the records: new lines become
owner statements, changed lines bump the record version, missing lines are forgotten (the row
and its full-text index entry are deleted). The whole document carries a version derived from
every record's ID and version, so a save based on a stale view is a 409.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.core.canonical import sha256_hex
from opa.core.clock import Clock
from opa.core.errors import Conflict, Invalid, NotFound
from opa.db.models import AuditLog, Conversation, Memory, Owner
from opa.db.outbox import emit

MAX_TEXT = 1000
MAX_ITEMS = 500
TAG_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_ITEM = re.compile(r"^\s*[-*]\s+(?:\[(?P<tags>[^\]]*)\]\s+)?(?P<text>.*?)\s*(?:<!--\s*m:(?P<id>[0-9A-Z]{26})\s*-->)?\s*$")
_SCOPE = re.compile(r"<!--\s*scope:(?P<id>[0-9A-Z]{26})\s*-->")
FORGET_NOTE = ("Forgetting deletes the memory and its search index entry right away. The conversations and files it came "
               "from are kept until you delete them separately.")


def clean_text(text: str) -> str:
    t = " ".join((text or "").split())
    if not t:
        raise Invalid("memory text must not be empty")
    if len(t) > MAX_TEXT:
        raise Invalid(f"memory text is limited to {MAX_TEXT} characters")
    if "<!--" in t or "-->" in t:
        raise Invalid("memory text cannot contain HTML comments")
    return t


def clean_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = t.strip().lower()
        if not t:
            continue
        if not TAG_RE.match(t):
            raise Invalid(f"tag {t!r} must be 1-32 lowercase letters, digits, - or _")
        if t not in out:
            out.append(t)
    if len(out) > 5:
        raise Invalid("at most 5 tags")
    return out


def collection_version(items: list[Memory]) -> str:
    return sha256_hex("|".join(f"{m.id}:{m.version}" for m in sorted(items, key=lambda m: m.id)))[:16]


@dataclass
class ParsedItem:
    id: str | None
    text: str
    tags: list[str]
    scope: str
    line: int


class MemoryService:
    def __init__(self, db: AsyncSession, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def _lock_owner(self, owner_id: str) -> None:
        await self.db.scalar(select(Owner.id).where(Owner.id == owner_id).with_for_update())

    async def confirmed(self, owner_id: str) -> list[Memory]:
        q = select(Memory).where(Memory.owner_id == owner_id, Memory.confirmed.is_(True)).order_by(Memory.created_at, Memory.id)
        return list(await self.db.scalars(q))

    async def suggestions(self, owner_id: str) -> list[Memory]:
        q = select(Memory).where(Memory.owner_id == owner_id, Memory.confirmed.is_(False)).order_by(Memory.created_at.desc(), Memory.id)
        return list(await self.db.scalars(q))

    async def get(self, owner_id: str, memory_id: str, *, for_update: bool = False) -> Memory:
        q = select(Memory).where(Memory.id == memory_id, Memory.owner_id == owner_id)
        m = await self.db.scalar(q.with_for_update() if for_update else q)
        if m is None:
            raise NotFound("memory not found")
        return m

    async def _check_scope(self, owner_id: str, scope: str) -> str:
        if scope == "global":
            return scope
        conv = await self.db.scalar(select(Conversation.id).where(Conversation.id == scope, Conversation.owner_id == owner_id, Conversation.deleted_at.is_(None)))
        if conv is None:
            raise Invalid("scope must be 'global' or one of your conversations")
        return scope

    # ------------------------------------------------------------ record operations
    async def add(self, owner_id: str, text: str, *, kind: str, confirmed: bool, scope: str = "global", tags: list[str] | None = None,
                  source_ref: dict[str, Any] | None = None, actor: str = "owner") -> Memory:
        await self._lock_owner(owner_id)
        count = len(await self.confirmed(owner_id)) + len(await self.suggestions(owner_id))
        if count >= MAX_ITEMS:
            raise Invalid(f"at most {MAX_ITEMS} memories; forget some first")
        now = self.clock.now()
        m = Memory(owner_id=owner_id, text=clean_text(text), kind=kind, confirmed=confirmed, scope=await self._check_scope(owner_id, scope),
                   tags=clean_tags(tags), source_ref=source_ref or {}, version=1, created_at=now, updated_at=now)
        self.db.add(m)
        await self.db.flush()
        self.db.add(AuditLog(owner_id=owner_id, actor=actor, type="memory.created" if confirmed else "memory.suggested", subject_id=m.id,
                             sanitized={"kind": kind, "scope": m.scope}))
        await emit(self.db, owner_id, "memory.updated", {"memory_id": m.id, "change": "created" if confirmed else "suggested"},
                   notify=None if confirmed else ("Suggested memory", "The assistant suggested something to remember. It is not used until you accept it."))
        return m

    async def update(self, owner_id: str, memory_id: str, expected_version: int, *, text: str | None = None, tags: list[str] | None = None,
                     scope: str | None = None) -> Memory:
        m = await self.get(owner_id, memory_id, for_update=True)
        if m.version != expected_version:
            raise Conflict(f"this memory is at version {m.version}, not {expected_version}; reload it", code="version_conflict")
        self._apply(m, text, tags, await self._check_scope(owner_id, scope) if scope is not None else None)
        await emit(self.db, owner_id, "memory.updated", {"memory_id": m.id, "change": "edited"})
        return m

    def _apply(self, m: Memory, text: str | None, tags: list[str] | None, scope: str | None) -> bool:
        changed = False
        if text is not None and clean_text(text) != m.text:
            m.text, changed = clean_text(text), True
        if tags is not None and clean_tags(tags) != list(m.tags):
            m.tags, changed = clean_tags(tags), True
        if scope is not None and scope != m.scope:
            m.scope, changed = scope, True
        if changed:
            m.version += 1
            m.updated_at = self.clock.now()
            self.db.add(AuditLog(owner_id=m.owner_id, actor="owner", type="memory.edited", subject_id=m.id, sanitized={"version": m.version}))
        return changed

    async def forget(self, owner_id: str, memory_id: str) -> None:
        m = await self.get(owner_id, memory_id, for_update=True)
        await self.db.delete(m)  # the generated tsvector and its GIN entry go with the row
        self.db.add(AuditLog(owner_id=owner_id, actor="owner", type="memory.forgotten", subject_id=memory_id, sanitized={"kind": m.kind}))
        await emit(self.db, owner_id, "memory.updated", {"memory_id": memory_id, "change": "forgotten"})

    async def accept(self, owner_id: str, memory_id: str, expected_version: int) -> Memory:
        m = await self.get(owner_id, memory_id, for_update=True)
        if m.version != expected_version:
            raise Conflict("this suggestion changed; reload it", code="version_conflict")
        if m.confirmed:
            return m
        m.confirmed = True
        m.version += 1
        m.updated_at = self.clock.now()
        self.db.add(AuditLog(owner_id=owner_id, actor="owner", type="memory.accepted", subject_id=m.id, sanitized={"kind": m.kind}))
        await emit(self.db, owner_id, "memory.updated", {"memory_id": m.id, "change": "accepted"})
        return m

    # ------------------------------------------------------------ Markdown editor
    async def markdown(self, owner_id: str) -> tuple[str, str]:
        items = await self.confirmed(owner_id)
        convs = {c.id: c.title for c in await self.db.scalars(select(Conversation).where(Conversation.owner_id == owner_id))}
        by_scope: dict[str, list[Memory]] = {}
        for m in items:
            by_scope.setdefault(m.scope, []).append(m)
        lines = ["# Memory", "", "## Global", ""]
        lines += [self._line(m) for m in by_scope.pop("global", [])]
        for scope, ms in by_scope.items():
            title = " ".join((convs.get(scope) or "deleted conversation").split()).replace("<!--", "").replace("-->", "")
            lines += ["", f"## Conversation: {title} <!-- scope:{scope} -->", ""]
            lines += [self._line(m) for m in ms]
        return "\n".join(lines).rstrip() + "\n", collection_version(items)

    @staticmethod
    def _line(m: Memory) -> str:
        tags = f"[{', '.join(m.tags)}] " if m.tags else ""
        return f"- {tags}{m.text} <!-- m:{m.id} -->"

    @staticmethod
    def parse(markdown: str) -> list[ParsedItem]:
        items: list[ParsedItem] = []
        scope = "global"
        for n, raw in enumerate(markdown.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith("## "):
                    sm = _SCOPE.search(line)
                    if sm:
                        scope = sm.group("id")
                    elif line[3:].strip().lower() == "global":
                        scope = "global"
                    else:
                        raise Invalid(f"line {n}: section headings are '## Global' or a conversation heading from this editor")
                continue
            m = _ITEM.match(raw)
            if not m or not m.group("text"):
                raise Invalid(f"line {n}: each memory is one list item starting with '- '")
            tags = [t for t in (m.group("tags") or "").split(",")] if m.group("tags") is not None else []
            items.append(ParsedItem(id=m.group("id"), text=m.group("text"), tags=tags, scope=scope, line=n))
        return items

    async def save_markdown(self, owner_id: str, markdown: str, expected_version: str) -> dict[str, int]:
        await self._lock_owner(owner_id)  # serializes editor saves per owner
        current = await self.confirmed(owner_id)
        if collection_version(current) != expected_version:
            raise Conflict("your memory changed since you opened the editor; reload it and apply your edits again", code="version_conflict")
        parsed = self.parse(markdown)
        if len(parsed) > MAX_ITEMS:
            raise Invalid(f"at most {MAX_ITEMS} memories")
        by_id = {m.id: m for m in current}
        seen: set[str] = set()
        stats = {"created": 0, "updated": 0, "forgotten": 0}
        for p in parsed:
            scope = await self._check_scope(owner_id, p.scope)
            if p.id is None:
                now = self.clock.now()
                m = Memory(owner_id=owner_id, text=clean_text(p.text), kind="user_statement", confirmed=True, scope=scope,
                           tags=clean_tags(p.tags), source_ref={"type": "memory_editor"}, version=1, created_at=now, updated_at=now)
                self.db.add(m)
                stats["created"] += 1
                continue
            if p.id not in by_id:
                raise Invalid(f"line {p.line}: unknown memory ID; remove the hidden comment to add it as a new item")
            if p.id in seen:
                raise Invalid(f"line {p.line}: this memory appears twice")
            seen.add(p.id)
            if self._apply(by_id[p.id], p.text, p.tags, scope):
                stats["updated"] += 1
        for mid, m in by_id.items():
            if mid not in seen:
                await self.db.delete(m)
                self.db.add(AuditLog(owner_id=owner_id, actor="owner", type="memory.forgotten", subject_id=mid, sanitized={"kind": m.kind}))
                stats["forgotten"] += 1
        await self.db.flush()
        if any(stats.values()):
            await emit(self.db, owner_id, "memory.updated", {"change": "editor", **stats})
        return stats
