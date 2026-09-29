"""v0.2 sharing: a conversation snapshot anyone with the link can read, and nothing more."""
from httpx import ASGITransport
from sqlalchemy import select, text

from opa.core.canonical import sha256_hex
from opa.db.models import Share
from tests.conftest import Client
from tests.helpers import script


async def chat(client, make_worker, text_, reply, rid):
    conv = await client.default_conversation()
    await client.submit(conv, text_, rid)
    assert await make_worker(script({"text": reply})).run_once()
    return conv


def token_of(url: str) -> str:
    assert url.startswith("http://127.0.0.1:8080/s/")
    return url.rsplit("/", 1)[1]


async def test_share_is_a_frozen_read_only_snapshot(client, anon, make_worker, sessionmaker):
    conv = await chat(client, make_worker, "Plan <b>my</b> week & <script>alert(1)</script>", "Here is your plan.", "r1")
    # an account card in the same conversation must never be shared
    req = {"action": "connect", "provider": "fixture", "connection_id": None, "modes": None, "reason": "To read your calendar."}
    await client.submit(conv, "connect", "r2")
    assert await make_worker(script({"tool_calls": [{"name": "connections__request", "input": req}]}, {"text": "Confirm the card."})).run_once()

    r = await client.post(f"/api/conversations/{conv}/shares", json={"title": "My week", "expires_in_days": 7})
    assert r.status_code == 201, r.text
    share, url = r.json()["share"], r.json()["url"]
    assert share["message_count"] == 4 and share["status"] == "active"  # two user messages, two replies; no card
    await chat(client, make_worker, "a later secret", "later reply", "r3")  # after sharing: not included

    page = await anon.get(f"/s/{token_of(url)}")
    assert page.status_code == 200
    body = page.text
    assert "My week" in body and "Here is your plan." in body and "Confirm the card." in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "<script" not in body and "&lt;b&gt;my&lt;/b&gt;" in body
    assert "To read your calendar" not in body and "Request: connect" not in body
    assert "a later secret" not in body and "later reply" not in body
    h = page.headers
    assert "default-src 'none'" in h["content-security-policy"] and "script-src" not in h["content-security-policy"]
    assert h["x-robots-tag"].startswith("noindex") and h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"
    assert "set-cookie" not in h
    shares = (await client.get("/api/shares")).json()
    assert shares[0]["view_count"] == 1 and "url" not in shares[0] and "token" not in str(shares[0])


async def test_only_chosen_messages_are_shared(client, make_worker, anon):
    conv = await chat(client, make_worker, "first question", "first answer", "r1")
    await chat(client, make_worker, "second question", "second answer", "r2")
    msgs = (await client.get(f"/api/conversations/{conv}/messages")).json()
    pick = [m["id"] for m in msgs if m["content"] in ("second question", "second answer")]
    r = await client.post(f"/api/conversations/{conv}/shares", json={"message_ids": pick})
    assert r.status_code == 201 and r.json()["share"]["message_count"] == 2
    body = (await anon.get(f"/s/{token_of(r.json()['url'])}")).text
    assert "second answer" in body and "first" not in body

    other = await client.post("/api/conversations", json={"title": "Side"})
    bad = await client.post(f"/api/conversations/{other.json()['id']}/shares", json={"message_ids": pick})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_message"
    empty = await client.post(f"/api/conversations/{other.json()['id']}/shares", json={})
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "nothing_to_share"


async def test_revoked_expired_and_unknown_links_look_the_same(client, make_worker, anon, sessionmaker, clock):
    conv = await chat(client, make_worker, "hello", "hi", "r1")
    a = (await client.post(f"/api/conversations/{conv}/shares", json={"expires_in_days": 1})).json()
    b = (await client.post(f"/api/conversations/{conv}/shares", json={})).json()
    r = await client.delete(f"/api/shares/{b['share']['id']}")
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    async with sessionmaker() as db:
        assert (await db.get(Share, b["share"]["id"])).snapshot == []  # revoking deletes the copy

    revoked = await anon.get(f"/s/{token_of(b['url'])}")
    unknown = await anon.get("/s/" + "x" * 43)
    assert revoked.status_code == unknown.status_code == 404 and revoked.text == unknown.text
    assert (await anon.get(f"/s/{token_of(a['url'])}")).status_code == 200
    clock.advance(days=1, seconds=1)
    expired = await anon.get(f"/s/{token_of(a['url'])}")
    assert expired.status_code == 404 and expired.text == unknown.text
    assert [s["status"] for s in (await client.get("/api/shares")).json()] == ["revoked", "expired"]

    from opa.api.routers.shares import clear_expired
    assert await clear_expired(sessionmaker, clock.now()) == 1
    async with sessionmaker() as db:
        assert (await db.get(Share, a["share"]["id"])).snapshot == []


async def test_tokens_are_stored_hashed_and_shares_are_owner_bound(client, make_worker, sessionmaker, app, other_owner):
    conv = await chat(client, make_worker, "hello", "hi", "r1")
    created = (await client.post(f"/api/conversations/{conv}/shares", json={})).json()
    token = token_of(created["url"])
    assert len(token) >= 43  # 32 random bytes
    async with sessionmaker() as db:
        row = (await db.scalars(select(Share))).one()
        dump = str((await db.execute(text("SELECT row_to_json(s)::text FROM shares s"))).scalar())
    assert row.token_hash == sha256_hex(token) and token not in dump
    async with Client(transport=ASGITransport(app=app), base_url="http://test") as other:
        await other.sign_in("other@example.com", "another long password")
        assert (await other.get("/api/shares")).json() == []
        assert (await other.delete(f"/api/shares/{created['share']['id']}")).status_code == 404
        assert (await other.post(f"/api/conversations/{conv}/shares", json={})).status_code == 404


async def test_delete_all_turns_off_every_link(client, make_worker, anon):
    conv = await chat(client, make_worker, "hello", "hi", "r1")
    url = (await client.post(f"/api/conversations/{conv}/shares", json={})).json()["url"]
    me = (await client.get("/api/me")).json()
    r = await client.post("/api/delete-all", json={"confirm": me["email"]})
    assert r.status_code in (200, 202), r.text
    assert (await anon.get(f"/s/{token_of(url)}")).status_code == 404
    assert (await client.get("/api/shares")).json() == []
