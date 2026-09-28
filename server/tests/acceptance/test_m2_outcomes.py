"""M2: unknown outcomes and reconciliation (A04, A05), calendar rules (CAL-01), mail filtering (GMAIL-01)."""
import json

import pytest

from opa.sdk.contract import ConnectorError
from tests.helpers import (action_of, call, connect, decide, fixture_state, mutate_fixture, pending_approval, script,
                           send_call, sent_messages, set_fault, task_state, tool_results, when)


async def _run_to_approval(client, make_worker, steps, intent="do it"):
    conv = await client.default_conversation()
    sub = await client.submit(conv, intent, "r1")
    w = make_worker(script(*steps))
    assert await w.run_once()
    return sub["task_id"], w


async def test_a04_lost_response_blocks_and_never_resends(client, make_worker, sessionmaker, clock):
    conn = await connect(client, gmail="read_write")
    await set_fault(sessionmaker, clock, conn["id"], "gmail.send_message", "lose_response")
    task_id, w = await _run_to_approval(client, make_worker, [{"tool_calls": [send_call(conn["id"])]}, {"text": "Sent."}])
    await decide(client, await pending_approval(client))
    assert await w.run_once()

    assert len(await sent_messages(sessionmaker, clock, conn["id"])) == 1  # the provider did accept it
    action = await action_of(sessionmaker, task_id)
    assert action.state == "unknown"
    t = await task_state(client, task_id)
    assert t["state"] == "blocked" and t["blocker_code"] == "unknown_external_outcome"
    notes = [n["title"] for n in (await client.get("/api/notifications")).json()]
    assert "Outcome unknown" in notes

    r = await client.post(f"/api/tasks/{task_id}/resume")
    assert r.status_code == 409 and r.json()["error"]["code"] == "reconcile_required"
    assert await w.run_once() is False  # nothing retries on its own

    r = await client.post(f"/api/actions/{action.id}/reconcile")
    assert r.json()["state"] == "succeeded" and r.json()["provider_refs"]
    assert (await client.post(f"/api/tasks/{task_id}/resume")).status_code == 200
    assert await w.run_once()
    assert (await task_state(client, task_id))["state"] == "succeeded"
    assert len(await sent_messages(sessionmaker, clock, conn["id"])) == 1  # still exactly one


async def test_a04_not_found_needs_owner_answer_and_does_not_resend(client, make_worker, sessionmaker, clock):
    conn = await connect(client, gmail="read_write")
    await set_fault(sessionmaker, clock, conn["id"], "gmail.send_message", "lose_request")
    task_id, w = await _run_to_approval(client, make_worker, [{"tool_calls": [send_call(conn["id"])]}, {"text": "Could not confirm the email."}])
    await decide(client, await pending_approval(client))
    assert await w.run_once()
    action = await action_of(sessionmaker, task_id)
    assert action.state == "unknown"

    r = await client.post(f"/api/actions/{action.id}/reconcile")
    assert r.json()["state"] == "unknown"  # not found in Sent: the broker will not guess
    r = await client.post(f"/api/actions/{action.id}/resolve", json={"outcome": "did_not_happen"})
    assert r.json()["state"] == "failed" and r.json()["resolution"] == "owner_confirmed_not_applied"
    assert (await client.post(f"/api/actions/{action.id}/resolve", json={"outcome": "happened"})).status_code == 409

    await client.post(f"/api/tasks/{task_id}/resume")
    assert await w.run_once()
    assert (await task_state(client, task_id))["state"] == "succeeded"
    assert await sent_messages(sessionmaker, clock, conn["id"]) == []
    assert json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])["status"] == "failed"


def _create(conn_id, start="2026-09-29T10:00:00", end="2026-09-29T11:00:00", **over):
    args = {"calendar_id": "primary", "summary": "Focus block", "description": None, "location": None,
            "start": start if isinstance(start, dict) else when(start), "end": end if isinstance(end, dict) else when(end), "confirm_past": False}
    args.update(over)
    return call("calendar.create_event", conn_id, **args)


async def test_a05_event_create_recovers_by_persisted_id(client, make_worker, sessionmaker, clock):
    conn = await connect(client, calendar="read_write")
    await set_fault(sessionmaker, clock, conn["id"], "calendar.create_event", "lose_response")
    task_id, w = await _run_to_approval(client, make_worker, [{"tool_calls": [_create(conn["id"])]}, {"text": "Created."}])
    ap = await pending_approval(client)
    fields = {f["label"]: f["value"] for f in ap["preview"]["fields"]}
    assert fields["Starts"] == "2026-09-29T10:00:00 (America/Los_Angeles)" and fields["Invitations"] == "No invitations are sent"
    await decide(client, ap)
    assert await w.run_once()
    action = await action_of(sessionmaker, task_id)
    assert action.state == "unknown" and action.provider_idempotency_key

    r = await client.post(f"/api/actions/{action.id}/reconcile")
    assert r.json()["state"] == "succeeded"
    events = (await fixture_state(sessionmaker, clock, conn["id"]))["events"]["primary"]
    assert [e["id"] for e in events].count(action.provider_idempotency_key) == 1
    await client.post(f"/api/tasks/{task_id}/resume")
    assert await w.run_once()
    assert (await task_state(client, task_id))["state"] == "succeeded"
    assert len((await fixture_state(sessionmaker, clock, conn["id"]))["events"]["primary"]) == 5  # 4 seeded + 1


async def test_a05_lookup_miss_marks_failed_safely(client, make_worker, sessionmaker, clock):
    conn = await connect(client, calendar="read_write")
    await set_fault(sessionmaker, clock, conn["id"], "calendar.create_event", "lose_request")
    task_id, w = await _run_to_approval(client, make_worker, [{"tool_calls": [_create(conn["id"])]}, {"text": "Not created."}])
    await decide(client, await pending_approval(client))
    assert await w.run_once()
    action = await action_of(sessionmaker, task_id)
    r = await client.post(f"/api/actions/{action.id}/reconcile")
    assert r.json()["state"] == "failed" and "not created" in r.json()["error"]["message"]


async def test_cal01_timed_event_keeps_local_time(client, make_worker, sessionmaker, clock):
    conn = await connect(client, calendar="read_write")
    task_id, w = await _run_to_approval(client, make_worker, [{"tool_calls": [_create(conn["id"])]}, {"text": "Created."}])
    await decide(client, await pending_approval(client))
    assert await w.run_once()
    action = await action_of(sessionmaker, task_id)
    ev = next(e for e in (await fixture_state(sessionmaker, clock, conn["id"]))["events"]["primary"] if e["id"] == action.provider_idempotency_key)
    assert ev["start"] == when("2026-09-29T10:00:00") and action.provider_refs == [f"calendar:primary/{ev['id']}"]


@pytest.mark.parametrize("over, fragment", [
    ({"start": when("2026-09-20T10:00:00"), "end": when("2026-09-20T11:00:00")}, "confirm_past"),
    ({"end": when("2026-09-29T09:00:00")}, "end must be after start"),
    ({"start": when("2026-03-08T02:30:00"), "end": when("2026-03-08T03:30:00"), "confirm_past": True}, "daylight saving gap"),
    ({"start": when("2026-09-29T10:00:00", "Mars/Olympus")}, "IANA"),
])
async def test_cal01_invalid_events_are_rejected_before_approval(client, make_worker, sessionmaker, over, fragment):
    conn = await connect(client, calendar="read_write")
    task_id, _ = await _run_to_approval(client, make_worker, [{"tool_calls": [_create(conn["id"], **over)]}, {"text": "Could not create it."}])
    assert (await client.get("/api/approvals")).json() == []
    assert (await task_state(client, task_id))["state"] == "succeeded"
    err = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])["error"]
    assert err["code"] == "invalid_input" and fragment in err["message"]


def _update(conn_id, event_id, **changes):
    base = {"summary": None, "description": None, "location": None, "start": None, "end": None}
    return call("calendar.update_event", conn_id, calendar_id="primary", event_id=event_id, changes={**base, **changes})


@pytest.mark.parametrize("event_id, fragment", [("evweekly1on1_20261001", "recurring"), ("evteamsync0001", "attendees"), ("nope", "not found")])
async def test_cal01_update_restrictions(client, make_worker, sessionmaker, event_id, fragment):
    conn = await connect(client, calendar="read_write")
    task_id, _ = await _run_to_approval(client, make_worker, [{"tool_calls": [_update(conn["id"], event_id, summary="x")]}, {"text": "ok"}])
    assert (await client.get("/api/approvals")).json() == []
    assert fragment in json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])["error"]["message"]


async def test_cal01_update_shows_diff_and_rejects_stale_edit(client, make_worker, sessionmaker, clock):
    conn = await connect(client, calendar="read_write")
    task_id, w = await _run_to_approval(client, make_worker, [{"tool_calls": [_update(conn["id"], "evdesignreview1", summary="Design review (moved)")]}, {"text": "Done."}])
    ap = await pending_approval(client)
    diff = next(f for f in ap["preview"]["fields"] if f["label"] == "Summary")
    assert (diff["before"], diff["value"]) == ("Design review", "Design review (moved)")
    await decide(client, ap)

    def someone_else_edits(state):
        ev = next(e for e in state["events"]["primary"] if e["id"] == "evdesignreview1")
        ev["etag"], ev["location"] = '"changed"', "Room 4"
    await mutate_fixture(sessionmaker, clock, conn["id"], someone_else_edits)
    assert await w.run_once()
    action = await action_of(sessionmaker, task_id)
    assert action.state == "failed" and action.error["code"] == "conflict"
    ev = next(e for e in (await fixture_state(sessionmaker, clock, conn["id"]))["events"]["primary"] if e["id"] == "evdesignreview1")
    assert ev["summary"] == "Design review"


async def test_gmail01_sensitive_mail_is_withheld_and_bodies_marked_untrusted(client, make_worker, sessionmaker):
    conn = await connect(client)
    steps = [{"tool_calls": [call("gmail.search_messages", conn["id"], query="", max_results=None, page_token=None)]},
             {"tool_calls": [call("gmail.get_message", conn["id"], message_id="m003")]},
             {"tool_calls": [call("gmail.get_message", conn["id"], message_id="m004")]},
             {"text": "Summary ready."}]
    task_id, _ = await _run_to_approval(client, make_worker, steps)
    results = [json.loads(r["content"]) for r in await tool_results(sessionmaker, task_id)]
    search, otp, normal = results
    assert search["data"]["filtered_count"] == 2
    assert {m["id"] for m in search["data"]["messages"]} == {"m001", "m002", "m004"}
    assert all(m["snippet"]["untrusted"] is True for m in search["data"]["messages"])
    assert otp["error"]["code"] == "permission_denied"
    assert normal["data"]["message"]["body"]["untrusted"] is True
    assert "482913" not in json.dumps(results)


async def test_read_only_mode_hides_writes_and_broker_refuses_them(client, make_worker, broker_client):
    conn = await connect(client)  # read_only
    task_id, w = await _run_to_approval(client, make_worker, [{"tool_calls": [send_call(conn["id"])]}, {"text": "Could not send."}])
    assert (await client.get("/api/approvals")).json() == []
    conv = await client.default_conversation()
    probe = await client.submit(conv, "probe", "r2")
    claimed, _, token = await w.claim()
    assert claimed == probe["task_id"]
    names = [t["name"] for t in await broker_client.tools(claimed, token)]
    assert "gmail__search_messages" in names and "gmail__send_message" not in names
    with pytest.raises(ConnectorError) as e:
        await broker_client.prepare(claimed, token, conn["id"], "gmail.send_message", send_call(conn["id"])["input"] | {})
    assert e.value.code in ("permission_denied", "invalid_input")
    args = {k: v for k, v in send_call(conn["id"])["input"].items() if k != "connection_id"}
    with pytest.raises(ConnectorError) as e:
        await broker_client.prepare(claimed, token, conn["id"], "gmail.send_message", args)
    assert e.value.code == "permission_denied" and "read-only" in e.value.message


async def test_transient_read_failure_is_retried(client, make_worker, sessionmaker, clock):
    conn = await connect(client)
    await set_fault(sessionmaker, clock, conn["id"], "calendar.list_calendars", "unavailable")
    task_id, _ = await _run_to_approval(client, make_worker, [{"tool_calls": [call("calendar.list_calendars", conn["id"])]}, {"text": "ok"}])
    result = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])
    assert result["status"] == "succeeded", result
    assert len(result["data"]["calendars"]) == 2
