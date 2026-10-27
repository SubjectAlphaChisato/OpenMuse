from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import select

from opa.api.deps import ClockDep, DbDep, OwnerDep, SettingsDep, get_owned
from opa.api.schemas import ConversationIn, ConversationOut, MessageAccepted, MessageIn, MessageOut
from opa.api.tasks_service import add_message, create_task, requeue
from opa.connectors.requests import describe_many
from opa.core.canonical import sha256_hex
from opa.core.errors import Conflict
from opa.db.models import Conversation, ConversationMember, Message, Owner, Task
from opa.db.outbox import emit

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


async def new_conversation(db, owner: Owner, title: str, *, is_default: bool) -> Conversation:
    """Create a conversation; its creator is its first (and, until shared conversations arrive, only) member."""
    conv = Conversation(owner_id=owner.id, title=title, is_default=is_default)
    db.add(conv)
    await db.flush()
    db.add(ConversationMember(conversation_id=conv.id, user_id=owner.id, role="owner", added_by=owner.id))
    await db.flush()
    return conv


async def ensure_default(db, owner: Owner) -> Conversation:
    conv = await db.scalar(select(Conversation).where(Conversation.owner_id == owner.id, Conversation.is_default.is_(True)))
    if conv is None:
        conv = await new_conversation(db, owner, "Main", is_default=True)
    return conv


@router.get("", response_model=list[ConversationOut])
async def list_conversations(db: DbDep, owner: OwnerDep) -> list[ConversationOut]:
    await ensure_default(db, owner)
    rows = await db.scalars(
        select(Conversation).where(Conversation.owner_id == owner.id, Conversation.deleted_at.is_(None)).order_by(Conversation.is_default.desc(), Conversation.created_at)
    )
    return [ConversationOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(body: ConversationIn, db: DbDep, owner: OwnerDep) -> ConversationOut:
    conv = await new_conversation(db, owner, body.title, is_default=False)
    return ConversationOut.model_validate(conv, from_attributes=True)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, db: DbDep, owner: OwnerDep, clock: ClockDep) -> Response:
    conv = await get_owned(db, Conversation, conversation_id, owner)
    if conv.is_default:
        raise Conflict("the default conversation cannot be deleted", code="default_conversation")
    conv.deleted_at = clock.now()
    from opa.api.actions_service import invalidate_actions, open_actions_for_task
    from opa.api.tasks_service import transition

    for task in list(await db.scalars(select(Task).where(Task.conversation_id == conv.id, Task.state.notin_(("succeeded", "failed", "cancelled"))))):
        await invalidate_actions(db, await open_actions_for_task(db, task.id), "the conversation was deleted", clock)
        await transition(db, task, "cancelled", clock)
    await emit(db, owner.id, "conversation.deleted", {"conversation_id": conv.id})
    return Response(status_code=204)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(conversation_id: str, db: DbDep, owner: OwnerDep, clock: ClockDep, after_seq: int = 0, limit: int = 200) -> list[MessageOut]:
    conv = await get_owned(db, Conversation, conversation_id, owner)
    limit = max(1, min(limit, 500))
    rows = await db.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.seq > after_seq).order_by(Message.seq).limit(limit)
    )
    rows = list(rows)
    cards = await describe_many(db, [r.connection_request_id for r in rows if r.connection_request_id], clock.now())
    return [MessageOut.model_validate(r, from_attributes=True).model_copy(update={"connection_request": cards.get(r.connection_request_id or "")})
            for r in rows]


@router.post("/{conversation_id}/messages", response_model=MessageAccepted, status_code=202)
async def post_message(
    conversation_id: str, body: MessageIn, db: DbDep, owner: OwnerDep, settings: SettingsDep, clock: ClockDep
) -> MessageAccepted:
    conv = await get_owned(db, Conversation, conversation_id, owner)
    content_hash = sha256_hex(body.content + "\n" + (body.task_id or ""))
    existing = await db.scalar(
        select(Message).where(Message.owner_id == owner.id, Message.client_request_id == body.client_request_id)
    )
    if existing is not None:
        if existing.content_hash != content_hash:
            raise Conflict("client_request_id already used with a different payload", code="duplicate_request")
        return MessageAccepted(message_id=existing.id, task_id=existing.task_id or "", created=False)

    if body.task_id:
        task = await get_owned(db, Task, body.task_id, owner)
        if task.state != "waiting_user":
            raise Conflict("task is not waiting for a reply", code="invalid_state")
        msg = await add_message(db, conv, "user", body.content, task_id=task.id, client_request_id=body.client_request_id, content_hash=content_hash)
        task.checkpoint = {**task.checkpoint, "pending_user_reply": body.content}
        await requeue(db, task, clock)
        return MessageAccepted(message_id=msg.id, task_id=task.id, created=True)

    task = await create_task(db, conv, body.content, settings, clock)
    msg = await add_message(db, conv, "user", body.content, task_id=task.id, client_request_id=body.client_request_id, content_hash=content_hash)
    return MessageAccepted(message_id=msg.id, task_id=task.id, created=True)
