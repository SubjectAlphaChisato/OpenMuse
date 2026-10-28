"""CRM accounts step 2: the assistant asks for team changes in the chat; only an admin's click adds access."""
from opa.api.auth import hash_password
from opa.db.models import User
from tests.helpers import script

PW = "a long enough password"


def ask(action, **fields):
    base = {"email": None, "name": None, "role": None, "partner_id": None, "user_email": None, "client_name": None, "kind": None,
            "can_edit": None, "initial_assignments": None, "reason": "The owner asked for this."}
    return {"tool_calls": [{"name": "accounts__request", "input": {"action": action, **base, **fields}}]}


async def cards(client, conv):
    return [m["account_request"] for m in (await client.get(f"/api/conversations/{conv}/messages")).json() if m["account_request"]]


async def test_invite_from_chat_waits_for_the_admin_and_the_link_is_returned_only_to_the_click(client, make_worker, sessionmaker):
    conv = await client.default_conversation()
    await client.submit(conv, "invite xiaowang@example.com as an advisor", "r1")
    worker = make_worker(script({"tool_calls": [{"name": "accounts__list", "input": {}}]},
                                ask("invite", email="xiaowang@example.com", name="小王", role="advisor"), {"text": "The card is in the chat."}))
    assert await worker.run_once()
    import json
    from tests.helpers import tool_results
    out = [json.loads(r["content"].split("\n", 1)[0]) for r in await tool_results(sessionmaker, (await client.get("/api/tasks")).json()["items"][0]["id"])]
    assert out[0]["data"]["you"] == {"email": "owner@example.com", "role": "admin"} and out[0]["data"]["team"][0]["role"] == "admin"
    assert out[1]["data"]["status"] == "waiting_for_admin" and "invite" not in json.dumps(out[1]).lower().replace("invite\"", "")  # no link for the model
    [card] = await cards(client, conv)
    assert card["status"] == "pending" and card["title"] == "邀请 小王 加入" and card["lines"][:2] == ["邮箱：xiaowang@example.com", "角色：顾问"]
    assert card["requested_by"] == "owner@example.com" and card["reason"] == "The owner asked for this."
    assert (await client.get("/api/team")).json()[0]["email"] == "owner@example.com" and len((await client.get("/api/team")).json()) == 1

    r = await client.post(f"/api/account-requests/{card['id']}/confirm")
    assert r.status_code == 200 and r.json()["invite_url"].startswith("http://127.0.0.1:8080/invite/")
    [card] = await cards(client, conv)
    assert card["status"] == "completed" and "xiaowang@example.com" in card["result"]


async def test_an_admins_reduction_from_chat_applies_at_once_and_the_card_records_it(client, make_worker, sessionmaker):
    async with sessionmaker() as db, db.begin():
        db.add(User(email="a1@example.com", name="a1", role="advisor", password_hash=hash_password(PW)))
    conv = await client.default_conversation()
    await client.submit(conv, "suspend a1", "r1")
    assert await make_worker(script(ask("suspend", user_email="a1@example.com", reason="On leave."), {"text": "Done."})).run_once()
    [card] = await cards(client, conv)
    assert card["status"] == "completed" and card["requires_admin"] is False and card["title"] == "暂停 a1 的账号"
    async with sessionmaker() as db:
        from sqlalchemy import select
        assert (await db.scalar(select(User.status).where(User.email == "a1@example.com"))) == "suspended"


async def test_a_bad_request_from_the_model_fails_without_a_card(client, make_worker, sessionmaker):
    conv = await client.default_conversation()
    await client.submit(conv, "invite someone", "r1")
    assert await make_worker(script(ask("invite", email="not-an-email", role="advisor"), {"text": "Sorry."})).run_once()
    assert await cards(client, conv) == []
    import json
    from tests.helpers import tool_results
    out = [json.loads(r["content"].split("\n", 1)[0]) for r in await tool_results(sessionmaker, (await client.get("/api/tasks")).json()["items"][0]["id"])]
    assert out[0]["status"] == "failed" and "email" in out[0]["error"]["message"]
