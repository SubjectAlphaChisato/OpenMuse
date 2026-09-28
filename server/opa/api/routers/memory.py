"""Memory editor (MEM-01): inspect, add, edit, forget, accept suggestions, and a Markdown view
with optimistic concurrency."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from opa.api.deps import ClockDep, DbDep, OwnerDep
from opa.db.models import Memory
from opa.memory.service import FORGET_NOTE, MemoryService

router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryOut(BaseModel):
    id: str
    text: str
    kind: str
    scope: str
    tags: list[str]
    confirmed: bool
    source_ref: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class MemoryDoc(BaseModel):
    items: list[MemoryOut]
    suggestions: list[MemoryOut]
    markdown: str
    version: str
    forget_note: str = FORGET_NOTE


class MarkdownIn(BaseModel):
    markdown: str = Field(max_length=600_000)
    expected_version: str


class MemoryIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    scope: str = "global"
    tags: list[str] = []


class MemoryPatch(BaseModel):
    expected_version: int
    text: str | None = Field(default=None, max_length=1000)
    tags: list[str] | None = None
    scope: str | None = None


class AcceptIn(BaseModel):
    expected_version: int


def _o(m: Memory) -> MemoryOut:
    return MemoryOut(id=m.id, text=m.text, kind=m.kind, scope=m.scope, tags=list(m.tags), confirmed=m.confirmed, source_ref=m.source_ref,
                     version=m.version, created_at=m.created_at, updated_at=m.updated_at)


async def _doc(svc: MemoryService, owner_id: str) -> MemoryDoc:
    md, version = await svc.markdown(owner_id)
    return MemoryDoc(items=[_o(m) for m in await svc.confirmed(owner_id)], suggestions=[_o(m) for m in await svc.suggestions(owner_id)],
                     markdown=md, version=version)


@router.get("", response_model=MemoryDoc)
async def get_memory(db: DbDep, owner: OwnerDep, clock: ClockDep) -> MemoryDoc:
    return await _doc(MemoryService(db, clock), owner.id)


@router.put("", response_model=MemoryDoc)
async def put_memory(body: MarkdownIn, db: DbDep, owner: OwnerDep, clock: ClockDep) -> MemoryDoc:
    """Replace the confirmed memory with the edited Markdown. 409 `version_conflict` if it changed meanwhile."""
    svc = MemoryService(db, clock)
    await svc.save_markdown(owner.id, body.markdown, body.expected_version)
    await db.flush()
    return await _doc(svc, owner.id)


@router.post("", response_model=MemoryOut, status_code=201)
async def add_memory(body: MemoryIn, db: DbDep, owner: OwnerDep, clock: ClockDep) -> MemoryOut:
    m = await MemoryService(db, clock).add(owner.id, body.text, kind="user_statement", confirmed=True, scope=body.scope, tags=body.tags,
                                          source_ref={"type": "memory_editor"})
    return _o(m)


@router.get("/{memory_id}", response_model=MemoryOut)
async def get_item(memory_id: str, db: DbDep, owner: OwnerDep, clock: ClockDep) -> MemoryOut:
    return _o(await MemoryService(db, clock).get(owner.id, memory_id))


@router.patch("/{memory_id}", response_model=MemoryOut)
async def edit_item(memory_id: str, body: MemoryPatch, db: DbDep, owner: OwnerDep, clock: ClockDep) -> MemoryOut:
    return _o(await MemoryService(db, clock).update(owner.id, memory_id, body.expected_version, text=body.text, tags=body.tags, scope=body.scope))


@router.post("/{memory_id}/accept", response_model=MemoryOut)
async def accept(memory_id: str, body: AcceptIn, db: DbDep, owner: OwnerDep, clock: ClockDep) -> MemoryOut:
    return _o(await MemoryService(db, clock).accept(owner.id, memory_id, body.expected_version))


@router.delete("/{memory_id}", status_code=204)
async def forget(memory_id: str, db: DbDep, owner: OwnerDep, clock: ClockDep) -> Response:
    """Forget (or reject a suggestion): the record and its index entry are deleted immediately."""
    await MemoryService(db, clock).forget(owner.id, memory_id)
    return Response(status_code=204)
