"""CRM phase 1 reservations for shared conversations (phase 2): conversation members and message authors."""
from sqlalchemy import select

from opa.db.models import ConversationMember, Message
from tests.helpers import script


async def test_every_conversation_has_its_creator_as_member_and_user_messages_record_their_author(client, make_worker, sessionmaker, owner):
    main = await client.default_conversation()
    side = (await client.post("/api/conversations", json={"title": "Side"})).json()["id"]
    await client.submit(main, "hello", "r1")
    assert await make_worker(script({"text": "hi"})).run_once()

    async with sessionmaker() as db:
        members = list(await db.scalars(select(ConversationMember).order_by(ConversationMember.joined_at)))
        assert [(m.conversation_id, m.user_id, m.role, m.left_at) for m in members] == [(main, owner.id, "owner", None), (side, owner.id, "owner", None)]
        msgs = list(await db.scalars(select(Message).where(Message.conversation_id == main).order_by(Message.seq)))
        assert [(m.role, m.author_user_id) for m in msgs] == [("user", owner.id), ("assistant", None)]  # the assistant is not a user

    # Deleting a conversation is a soft delete: membership stays as history. Delete-all removes everything (ON DELETE CASCADE).
    assert (await client.delete(f"/api/conversations/{side}")).status_code == 204
    async with sessionmaker() as db:
        assert await db.scalar(select(ConversationMember.id).where(ConversationMember.conversation_id == side)) is not None
    assert (await client.post("/api/delete-all", json={"confirm": "owner@example.com"})).status_code == 200
    async with sessionmaker() as db:
        assert (await db.scalars(select(ConversationMember))).all() == []
