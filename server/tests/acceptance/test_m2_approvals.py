"""M2: APP-01 approvals, action ledger, broker enforcement. Acceptance tests A02, A03, A06 (partial), A14, A18."""
import asyncio
import json

from httpx import ASGITransport
from sqlalchemy import func, select, text

from opa.db.models import Approval, DispatchClaim
from tests.conftest import Client
from tests.helpers import (action_of, connect, decide, pending_approval, script, send_call, sent_messages, task_state,
                           tool_results)


async def _propose_send(client, make_worker, conn, **over):
    conv = await client.default_conversation()
    sub = await client.submit(conv, "Email Priya the metrics", "r1")
    w = make_worker(script({"tool_calls": [send_call(conn["id"], **over)]}, {"text": "Sent the email."}))
    assert await w.run_once()
    t = await task_state(client, sub["task_id"])
    assert t["state"] == "waiting_approval", t
    return sub["task_id"], w


async def test_send_happens_only_after_approval_and_exactly_as_shown(client, make_worker, sessionmaker, clock):
    conn = await connect(client, gmail="read_write")
    task_id, w = await _propose_send(client, make_worker, conn)
    assert await sent_messages(sessionmaker, clock, conn["id"]) == []  # nothing sent yet

    ap = await pending_approval(client)
    fields = {f["label"]: f["value"] for f in ap["preview"]["fields"]}
    assert fields["To"] == "priya@partner.example" and fields["Subject"] == "Metrics for Thursday"
    assert fields["Body"].endswith("42") and fields["Account"] == "demo@example.test"
    assert ap["preview"]["side_effects"].startswith("Sends an email")
    notes = (await client.get("/api/notifications")).json()
    assert notes[0]["title"] == "Approval needed"

    r = await decide(client, ap)
    assert r.status_code == 200 and r.json()["outcome"] == "approved"
    assert (await task_state(client, task_id))["state"] == "queued"
    assert await w.run_once()

    sent = await sent_messages(sessionmaker, clock, conn["id"])
    assert len(sent) == 1
    assert (sent[0]["to"], sent[0]["subject"], sent[0]["body"]) == ("priya@partner.example", "Metrics for Thursday", "Hi Priya, the metrics are attached below.\n42")
    t = await task_state(client, task_id)
    assert t["state"] == "succeeded"
    action = await action_of(sessionmaker, task_id)
    assert action.state == "succeeded" and action.provider_refs == [f"gmail:{sent[0]['id']}"]
    result = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])
    assert result["status"] == "succeeded" and result["data"]["message_id"] == sent[0]["id"]


async def test_a02_changed_payload_after_approval_is_rejected(client, make_worker, sessionmaker, clock):
    conn = await connect(client, gmail="read_write")
    task_id, w = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    assert (await decide(client, ap)).json()["outcome"] == "approved"
    async with sessionmaker() as db, db.begin():  # tamper with the stored payload after approval
        await db.execute(text("""UPDATE actions SET canonical_args = jsonb_set(canonical_args, '{to}', '["attacker@evil.example"]') WHERE id = :a"""), {"a": ap["action_id"]})
    assert await w.run_once()
    assert await sent_messages(sessionmaker, clock, conn["id"]) == []
    action = await action_of(sessionmaker, task_id)
    assert action.state == "cancelled"
    async with sessionmaker() as db:
        assert (await db.get(Approval, ap["id"])).state == "invalidated"
    result = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])
    assert result["error"]["code"] == "approval_required"


async def test_a02_changed_payload_before_decision_invalidates(client, make_worker, sessionmaker):
    conn = await connect(client, gmail="read_write")
    task_id, _ = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    async with sessionmaker() as db, db.begin():
        await db.execute(text("""UPDATE actions SET canonical_args = jsonb_set(canonical_args, '{body}', '"changed"') WHERE id = :a"""), {"a": ap["action_id"]})
    r = await decide(client, ap)
    assert r.json()["outcome"] == "invalidated"
    t = await task_state(client, task_id)
    assert t["state"] == "blocked" and t["blocker_code"] == "payload_changed"


async def test_a02_wrong_expected_hash_is_a_conflict(client, make_worker):
    conn = await connect(client, gmail="read_write")
    await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    r = await decide(client, ap, expected="0" * 64)
    assert r.status_code == 409 and r.json()["error"]["code"] == "payload_changed"
    assert (await pending_approval(client))["state"] == "pending"


async def test_a02_expired_approval_cannot_be_granted(client, make_worker, clock):
    conn = await connect(client, gmail="read_write")
    task_id, _ = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    clock.advance(hours=25)
    r = await decide(client, ap)
    assert r.json()["outcome"] == "expired"
    t = await task_state(client, task_id)
    assert t["state"] == "blocked" and t["blocker_code"] == "approval_expired"


async def test_a02_expiry_sweep_blocks_waiting_task(client, make_worker, clock, sessionmaker):
    conn = await connect(client, gmail="read_write")
    task_id, w = await _propose_send(client, make_worker, conn)
    clock.advance(hours=24, seconds=1)
    assert await w.recovery_sweep() == 1
    t = await task_state(client, task_id)
    assert t["state"] == "blocked" and t["blocker_code"] == "approval_expired"
    assert (await action_of(sessionmaker, task_id)).state == "expired"


async def test_a02_replayed_approval_sends_once(client, make_worker, sessionmaker, clock, broker_client):
    conn = await connect(client, gmail="read_write")
    task_id, w = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    assert (await decide(client, ap)).json()["outcome"] == "approved"
    assert (await decide(client, ap)).json()["outcome"] == "unchanged"
    assert await w.run_once()
    assert await w.run_once() is False
    assert len(await sent_messages(sessionmaker, clock, conn["id"])) == 1


async def test_a03_concurrent_dispatch_claims_once(client, make_worker, sessionmaker, clock, broker_client):
    conn = await connect(client, gmail="read_write")
    task_id, w = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    await decide(client, ap)
    claimed_task, _, token = await w.claim()
    assert claimed_task == task_id
    results = await asyncio.gather(*[broker_client.dispatch(ap["action_id"], task_id, token) for _ in range(5)])
    assert len(await sent_messages(sessionmaker, clock, conn["id"])) == 1
    assert sum(r.status == "succeeded" for r in results) >= 1
    assert all(r.status in ("succeeded", "unknown") for r in results)  # losers report the in-flight claim, never a second send
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(DispatchClaim)) == 1


async def test_a03_concurrent_decisions_grant_once(client, make_worker, sessionmaker):
    conn = await connect(client, gmail="read_write")
    task_id, _ = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    outcomes = sorted([(await r).json()["outcome"] for r in [asyncio.ensure_future(decide(client, ap)) for _ in range(3)]])
    assert outcomes.count("approved") == 1 and outcomes.count("unchanged") == 2
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Approval).where(Approval.state == "approved")) == 1


async def test_stale_worker_lease_cannot_dispatch(client, make_worker, sessionmaker, clock, broker_client):
    conn = await connect(client, gmail="read_write")
    task_id, w1 = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    await decide(client, ap)
    _, _, stale_token = await w1.claim()
    clock.advance(seconds=61)
    w2 = make_worker(script({"tool_calls": [send_call(conn["id"])]}, {"text": "Sent the email."}), name="w2")
    assert await w2.recovery_sweep() >= 1
    r = await broker_client.dispatch(ap["action_id"], task_id, stale_token)
    assert r.status == "failed" and r.error.code == "permission_denied"
    assert (await action_of(sessionmaker, task_id)).state == "approved"
    assert await w2.run_once()
    assert len(await sent_messages(sessionmaker, clock, conn["id"])) == 1


async def test_a06_disconnect_invalidates_pending_approval(client, make_worker, sessionmaker, clock):
    conn = await connect(client, gmail="read_write")
    task_id, w = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    r = await client.delete(f"/api/connections/{conn['id']}")
    assert r.status_code == 200 and r.json()["status"] == "disconnected"
    t = await task_state(client, task_id)
    assert t["state"] == "blocked" and t["blocker_code"] == "connection_disconnected"
    assert (await decide(client, ap)).json()["outcome"] == "unchanged"
    assert (await client.get("/api/approvals")).json() == []
    assert (await client.post(f"/api/tasks/{task_id}/resume")).status_code == 200
    assert await w.run_once()
    assert await sent_messages(sessionmaker, clock, conn["id"]) == []
    result = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])
    assert "cancelled" in result["error"]["message"]


async def test_mode_downgrade_invalidates_write_approval(client, make_worker):
    conn = await connect(client, gmail="read_write")
    task_id, _ = await _propose_send(client, make_worker, conn)
    r = await client.patch(f"/api/connections/{conn['id']}", json={"modes": {"gmail": "read_only"}, "expected_version": conn["version"]})
    assert r.status_code == 200
    t = await task_state(client, task_id)
    assert t["state"] == "blocked" and t["blocker_code"] == "permission_changed"
    stale = await client.patch(f"/api/connections/{conn['id']}", json={"modes": {"gmail": "off"}, "expected_version": conn["version"]})
    assert stale.status_code == 409


async def test_a18_cancel_while_awaiting_approval_prevents_dispatch(client, make_worker, sessionmaker, clock, broker_client):
    conn = await connect(client, gmail="read_write")
    task_id, w = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    assert (await client.post(f"/api/tasks/{task_id}/cancel")).json()["state"] == "cancelled"
    assert (await decide(client, ap)).json()["outcome"] == "unchanged"
    assert await w.run_once() is False
    r = await broker_client.dispatch(ap["action_id"], task_id, "any-token")
    assert r.status == "failed" and r.error.code == "approval_required"
    assert await sent_messages(sessionmaker, clock, conn["id"]) == []


async def test_denial_blocks_then_resume_continues_without_sending(client, make_worker, sessionmaker, clock):
    conn = await connect(client, gmail="read_write")
    task_id, w = await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    assert (await decide(client, ap, "deny")).json()["outcome"] == "denied"
    t = await task_state(client, task_id)
    assert t["state"] == "blocked" and t["blocker_code"] == "approval_denied"
    await client.post(f"/api/tasks/{task_id}/resume")
    assert await w.run_once()
    assert (await task_state(client, task_id))["state"] == "succeeded"
    assert await sent_messages(sessionmaker, clock, conn["id"]) == []
    assert json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])["error"]["code"] == "permission_denied"


async def test_always_ask_requires_approval_for_reads(client, make_worker, sessionmaker):
    conn = await connect(client)
    me = await client.patch("/api/me", json={"approval_mode": "always_ask"})
    assert me.json()["approval_mode"] == "always_ask"
    conv = await client.default_conversation()
    sub = await client.submit(conv, "What's in my inbox?", "r1")
    from tests.helpers import call
    w = make_worker(script({"tool_calls": [call("gmail.search_messages", conn["id"], query="in:inbox", max_results=10, page_token=None)]}, {"text": "Here is your inbox."}))
    assert await w.run_once()
    assert (await task_state(client, sub["task_id"]))["state"] == "waiting_approval"
    ap = await pending_approval(client)
    assert ap["effect"] == "read" and ap["preview"]["title"] == "Read: gmail.search_messages"
    await decide(client, ap)
    assert await w.run_once()
    assert (await task_state(client, sub["task_id"]))["state"] == "succeeded"
    data = json.loads((await tool_results(sessionmaker, sub["task_id"]))[-1]["content"])["data"]
    assert data["filtered_count"] == 2 and len(data["messages"]) == 2


async def test_a14_other_owner_cannot_touch_approvals_or_connections(app, client, other_owner, make_worker):
    conn = await connect(client, gmail="read_write")
    await _propose_send(client, make_worker, conn)
    ap = await pending_approval(client)
    async with Client(transport=ASGITransport(app=app), base_url="http://test") as other:
        await other.sign_in("other@example.com", "another long password")
        assert (await other.get("/api/approvals")).json() == []
        assert (await other.get(f"/api/approvals/{ap['id']}")).status_code == 404
        assert (await decide(other, ap)).status_code == 404
        assert (await other.get("/api/actions")).json() == []
        assert (await other.post(f"/api/actions/{ap['action_id']}/reconcile")).status_code == 404
        assert (await other.patch(f"/api/connections/{conn['id']}", json={"modes": {"gmail": "off"}, "expected_version": conn["version"]})).status_code == 404
        assert (await other.delete(f"/api/connections/{conn['id']}")).status_code == 404
        assert (await other.get("/api/connections")).json() == []
    assert (await pending_approval(client))["state"] == "pending"
