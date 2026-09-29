"""M5: schedules (AUTO-01, A08), calendar-change triggers (AUTO-02, A09), pausing (CON-01, A06), isolation (A14)."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport
from sqlalchemy import select

from opa.broker.fixture import touch_event
from opa.db.models import Automation, Connection, Occurrence, Task
from opa.scheduler.service import Scheduler
from tests.conftest import Client
from tests.helpers import call, connect, decide, mutate_fixture, pending_approval, script, send_call, set_fault, tool_results

UTC = timezone.utc


@pytest.fixture
def make_scheduler(settings, sessionmaker, clock, broker_client):
    return lambda: Scheduler(settings, sessionmaker, clock, broker_client)


async def create(client, **over):
    body = {"name": "Daily note", "kind": "daily", "prompt": "Say good evening", "time_zone": "UTC", "local_time": "18:00",
            "connection_ids": [], "confirm": True} | over
    r = await client.post("/api/automations", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def runs(sm, automation_id):
    async with sm() as db:
        tasks = list(await db.scalars(select(Task).where(Task.origin["automation_id"].astext == automation_id).order_by(Task.created_at)))
        occs = list(await db.scalars(select(Occurrence).where(Occurrence.automation_id == automation_id).order_by(Occurrence.scheduled_for)))
    return tasks, occs


async def titles(client):
    return [n["title"] for n in (await client.get("/api/notifications")).json()]


# ------------------------------------------------------------------ AUTO-01 basics

async def test_preview_normalizes_and_rejects_bad_input(client, clock):
    r = await client.post("/api/automations/preview", json={"name": "Brief", "kind": "weekly", "prompt": "x", "time_zone": "America/Los_Angeles",
                                                            "local_time": "18:00", "weekdays": ["mon", "tue", "wed", "thu", "fri"]})
    body = r.json()
    assert body["description"] == "Every weekday at 18:00 (America/Los_Angeles)"
    assert [u["local"] for u in body["upcoming"]] == ["Mon 2026-09-28 18:00 PDT", "Tue 2026-09-29 18:00 PDT", "Wed 2026-09-30 18:00 PDT"]
    assert body["normalized"]["weekdays"] == [0, 1, 2, 3, 4]
    bad_zone = await client.post("/api/automations/preview", json={"name": "x", "kind": "daily", "prompt": "x", "time_zone": "Mars/Base", "local_time": "18:00"})
    assert bad_zone.status_code == 422 and bad_zone.json()["error"]["code"] == "invalid_schedule"
    past = await client.post("/api/automations/preview", json={"name": "x", "kind": "once", "prompt": "x", "time_zone": "UTC", "local_time": "09:00", "run_date": "2026-09-27"})
    msg = past.json()["error"]["message"]
    assert past.status_code == 422 and "already passed" in msg and "Mon 2026-09-28 09:00" in msg


async def test_due_run_creates_one_task_and_repeated_ticks_do_not(client, clock, sessionmaker, make_scheduler):
    a = await create(client)
    s = make_scheduler()
    clock.set(datetime(2026, 9, 27, 17, 59, tzinfo=UTC))
    assert (await s.tick())["schedules"] == 0
    clock.set(datetime(2026, 9, 27, 18, 0, 5, tzinfo=UTC))
    await s.tick()
    await s.tick()
    await make_scheduler().tick()  # a restarted scheduler
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and [(o.state, o.late, o.missed_count) for o in occs] == [("started", False, 0)]
    t = tasks[0]
    assert t.origin["type"] == "schedule" and t.allowed_connection_ids == [] and t.intent.startswith("Say good evening")
    msgs = (await client.get(f"/api/conversations/{t.conversation_id}/messages")).json()
    assert msgs[-1]["role"] == "system" and msgs[-1]["content"].startswith("Scheduled: Daily note — Sun 2026-09-27 18:00")
    got = (await client.get(f"/api/automations/{a['id']}")).json()
    assert got["next_run_local"] == "Mon 2026-09-28 18:00 UTC" and got["recent"][0]["task_id"] == t.id


async def test_two_schedulers_at_once_create_one_task(client, clock, sessionmaker, make_scheduler):
    a = await create(client)
    clock.set(datetime(2026, 9, 27, 18, 0, 5, tzinfo=UTC))
    await asyncio.gather(*(make_scheduler().tick() for _ in range(4)))
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and len(occs) == 1


async def test_a08_missed_one_time_run_happens_once_and_is_marked_late(client, clock, sessionmaker, make_scheduler):
    a = await create(client, kind="once", run_date="2026-09-27", local_time="13:00")
    clock.set(datetime(2026, 9, 27, 16, 0, tzinfo=UTC))  # the service was down at 13:00
    await make_scheduler().tick()
    await make_scheduler().tick()
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and occs[0].late is True and "started late" in tasks[0].intent
    got = (await client.get(f"/api/automations/{a['id']}")).json()
    assert got["enabled"] is False and got["completed_at"] and got["next_run_at"] is None


async def test_a08_recurring_downtime_coalesces_into_one_run_with_missed_count(client, clock, sessionmaker, make_scheduler):
    a = await create(client)
    clock.set(datetime(2026, 9, 30, 19, 0, tzinfo=UTC))  # missed 27th, 28th, 29th; 30th is due
    await make_scheduler().tick()
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and len(occs) == 1
    assert occs[0].scheduled_for == datetime(2026, 9, 30, 18, 0, tzinfo=UTC) and occs[0].missed_count == 3 and occs[0].late
    assert "3 earlier run(s) were missed" in tasks[0].intent
    assert "Missed scheduled runs" in await titles(client)
    assert (await client.get(f"/api/automations/{a['id']}")).json()["next_run_local"] == "Thu 2026-10-01 18:00 UTC"


async def _resign(client, clock):
    await client.sign_in()  # the test jumps the clock beyond the session lifetime


async def test_a08_daylight_saving_transitions_through_real_ticks(client, clock, sessionmaker, make_scheduler, make_worker):
    clock.set(datetime(2026, 3, 8, 9, 0, tzinfo=UTC))  # 01:00 PST on the spring-forward day
    await _resign(client, clock)
    spring = await create(client, name="Spring", time_zone="America/Los_Angeles", local_time="02:30")
    clock.set(datetime(2026, 3, 8, 9, 59, tzinfo=UTC))
    await make_scheduler().tick()
    assert (await runs(sessionmaker, spring["id"]))[0] == []
    clock.set(datetime(2026, 3, 8, 10, 0, 30, tzinfo=UTC))  # 03:00:30 PDT: first valid time after the gap
    await make_scheduler().tick()
    tasks, occs = await runs(sessionmaker, spring["id"])
    assert len(tasks) == 1 and occs[0].scheduled_for == datetime(2026, 3, 8, 10, 0, tzinfo=UTC) and not occs[0].late

    clock.set(datetime(2026, 11, 1, 7, 0, tzinfo=UTC))  # 00:00 PDT on the fall-back day
    await _resign(client, clock)
    fall = await create(client, name="Fall", time_zone="America/Los_Angeles", local_time="01:30")
    clock.set(datetime(2026, 11, 1, 8, 30, 10, tzinfo=UTC))  # 01:30 PDT (first occurrence)
    await make_scheduler().tick()
    await make_worker().run_once()
    await make_worker().run_once()
    clock.set(datetime(2026, 11, 1, 9, 30, 10, tzinfo=UTC))  # 01:30 PST (the repeat)
    await make_scheduler().tick()
    tasks, occs = await runs(sessionmaker, fall["id"])
    assert len(tasks) == 1 and [o.scheduled_for for o in occs] == [datetime(2026, 11, 1, 8, 30, tzinfo=UTC)]
    assert (await client.get(f"/api/automations/{fall['id']}")).json()["next_run_at"].startswith("2026-11-02T09:30:00")


async def test_no_overlap_holds_then_runs_once_for_the_latest_due_time(client, clock, sessionmaker, make_scheduler, make_worker):
    a = await create(client)
    clock.set(datetime(2026, 9, 27, 18, 0, 5, tzinfo=UTC))
    await make_scheduler().tick()  # run 1 is queued and not yet done
    for day in (28, 29):
        clock.set(datetime(2026, 9, day, 18, 0, 5, tzinfo=UTC))
        await client.sign_in()
        await make_scheduler().tick()
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and [o.state for o in occs] == ["started", "pending", "pending"]
    assert (await titles(client)).count("Automation waiting") == 1
    assert await make_worker().run_once()  # run 1 finishes
    await make_scheduler().tick()
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 2 and [o.state for o in occs] == ["started", "coalesced", "started"]
    assert occs[2].missed_count == 1 and "1 earlier run(s) were missed" in tasks[1].intent


async def test_profile_time_zone_change_does_not_move_existing_schedules(client):
    a = await create(client, time_zone="America/Los_Angeles", local_time="09:00")
    r = await client.patch("/api/me", json={"time_zone": "Asia/Tokyo"})
    assert r.status_code == 200
    again = (await client.get(f"/api/automations/{a['id']}")).json()
    assert again["next_run_at"] == a["next_run_at"] and again["time_zone"] == "America/Los_Angeles"


# ------------------------------------------------------------------ connections, approvals, pausing

async def test_scheduled_run_is_limited_to_its_accounts_and_writes_still_need_approval(client, clock, sessionmaker, make_scheduler, make_worker, broker_client):
    chosen = await connect(client, gmail="read_write")
    other = await connect(client, gmail="read_write")
    a = await create(client, prompt="Email Priya the metrics", connection_ids=[chosen["id"]])
    clock.set(datetime(2026, 9, 27, 18, 0, 5, tzinfo=UTC))
    await make_scheduler().tick()
    [task], _ = await runs(sessionmaker, a["id"])
    assert task.allowed_connection_ids == [chosen["id"]]
    w = make_worker(script({"tool_calls": [send_call("@auto")]}, {"text": "Sent."}))
    claimed, _, token = await w.claim()
    tools = {t["name"]: t for t in await broker_client.tools(claimed, token)}
    assert tools["gmail__send_message"]["input_schema"]["properties"]["connection_id"]["enum"] == [chosen["id"]]
    from opa.sdk.contract import ConnectorError
    args = {k: v for k, v in send_call(other["id"])["input"].items() if k != "connection_id"}
    with pytest.raises(ConnectorError) as e:
        await broker_client.prepare(claimed, token, other["id"], "gmail.send_message", args)
    assert e.value.code == "permission_denied" and "may not use" in e.value.message
    clock.advance(seconds=61)  # release the probe's lease
    await w.recovery_sweep()
    assert await w.run_once()
    assert (await client.get(f"/api/tasks/{task.id}")).json()["state"] == "waiting_approval"  # never sent unattended
    ap = await pending_approval(client)
    assert ap["account"] == "demo@example.test"


async def test_disconnect_pauses_dependent_automation_and_due_runs_are_recorded(client, clock, sessionmaker, make_scheduler):
    conn = await connect(client)
    a = await create(client, connection_ids=[conn["id"]])
    await client.delete(f"/api/connections/{conn['id']}")
    got = (await client.get(f"/api/automations/{a['id']}")).json()
    assert got["paused_reason"] == "connection_disconnected" and "Automation paused" in await titles(client)
    clock.set(datetime(2026, 9, 27, 18, 0, 5, tzinfo=UTC))
    await make_scheduler().tick()
    tasks, occs = await runs(sessionmaker, a["id"])
    assert tasks == [] and [o.state for o in occs] == ["missed_paused"]
    again = await client.patch(f"/api/automations/{a['id']}", json={"expected_version": got["version"], "connection_ids": []})
    assert again.json()["paused_reason"] is None


async def test_signed_out_account_pauses_then_resumes(client, clock, sessionmaker, make_scheduler):
    conn = await connect(client)
    a = await create(client, connection_ids=[conn["id"]])
    async with sessionmaker() as db, db.begin():
        (await db.get(Connection, conn["id"])).status = "reauth_required"
    clock.set(datetime(2026, 9, 27, 18, 0, 5, tzinfo=UTC))
    await make_scheduler().tick()
    assert (await client.get(f"/api/automations/{a['id']}")).json()["paused_reason"] == "reauth_required"
    async with sessionmaker() as db, db.begin():
        (await db.get(Connection, conn["id"])).status = "connected"
    clock.set(datetime(2026, 9, 28, 18, 0, 5, tzinfo=UTC))
    await client.sign_in()
    await make_scheduler().tick()
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and [o.state for o in occs] == ["missed_paused", "started"]
    assert "Automation resumed" in await titles(client)


async def test_assistant_proposal_needs_owner_confirmation(client, clock, sessionmaker, make_scheduler, make_worker):
    conv = await client.default_conversation()
    await client.submit(conv, "Remind me every day at 19:00 to stretch", "r1")
    propose = call("automation.propose", "x", name="Stretch", kind="daily", prompt="Remind me to stretch", time_zone="UTC", local_time="19:00",
                   weekdays=None, run_date=None, connection_ids=[], calendar=None)
    propose["input"].pop("connection_id")
    past = call("automation.propose", "x", name="Old", kind="once", prompt="x", time_zone="UTC", local_time="08:00",
                weekdays=None, run_date="2026-09-27", connection_ids=[], calendar=None)
    past["input"].pop("connection_id")
    w = make_worker(script({"tool_calls": [propose]}, {"tool_calls": [past]}, {"text": "Proposed; please confirm it in Automations."}))
    assert await w.run_once()
    [auto] = (await client.get("/api/automations")).json()
    assert (auto["confirmed"], auto["enabled"], auto["created_by"]) == (False, False, "assistant")
    assert "Confirm a new automation" in await titles(client)
    async with sessionmaker() as db:
        task = (await db.scalars(select(Task))).one()
    results = [json.loads(r["content"]) for r in await tool_results(sessionmaker, task.id)]
    assert results[0]["data"]["status"] == "waiting_for_owner_confirmation"
    assert results[1]["error"]["code"] == "invalid_schedule" and "already passed" in results[1]["error"]["message"]

    clock.set(datetime(2026, 9, 27, 19, 0, 5, tzinfo=UTC))
    await make_scheduler().tick()
    assert (await runs(sessionmaker, auto["id"]))[0] == []  # unconfirmed never runs
    assert (await client.post(f"/api/automations/{auto['id']}/confirm", json={"expected_version": auto["version"] + 5})).status_code == 409
    ok = await client.post(f"/api/automations/{auto['id']}/confirm", json={"expected_version": auto["version"]})
    assert ok.json()["enabled"] is True and ok.json()["next_run_local"] == "Mon 2026-09-28 19:00 UTC"
    clock.set(datetime(2026, 9, 28, 19, 0, 5, tzinfo=UTC))
    await client.sign_in()
    await make_scheduler().tick()
    assert len((await runs(sessionmaker, auto["id"]))[0]) == 1


async def test_delete_cancels_unfinished_run(client, clock, sessionmaker, make_scheduler):
    a = await create(client)
    clock.set(datetime(2026, 9, 27, 18, 0, 5, tzinfo=UTC))
    await make_scheduler().tick()
    assert (await client.delete(f"/api/automations/{a['id']}")).status_code == 204
    [task], _ = await runs(sessionmaker, a["id"])
    assert task.state == "cancelled"
    assert (await client.get("/api/automations")).json() == []
    assert (await client.get(f"/api/automations/{a['id']}")).status_code == 404


async def test_a14_other_owner_cannot_see_or_change_automations(app, client, other_owner):
    a = await create(client)
    async with Client(transport=ASGITransport(app=app), base_url="http://test") as other:
        await other.sign_in("other@example.com", "another long password")
        assert (await other.get("/api/automations")).json() == []
        assert (await other.get(f"/api/automations/{a['id']}")).status_code == 404
        assert (await other.patch(f"/api/automations/{a['id']}", json={"expected_version": a["version"], "enabled": False})).status_code == 404
        assert (await other.post(f"/api/automations/{a['id']}/confirm", json={"expected_version": a["version"]})).status_code == 404
        assert (await other.delete(f"/api/automations/{a['id']}")).status_code == 404
        bad = await other.post("/api/automations", json={"name": "x", "kind": "daily", "prompt": "x", "time_zone": "UTC", "local_time": "09:00",
                                                         "connection_ids": [], "conversation_id": a["id"]})
        assert bad.status_code == 422


# ------------------------------------------------------------------ AUTO-02 calendar-change triggers (A09)

async def _trigger(client, conn, **over):
    return await create(client, name="Meeting brief refresh", kind="calendar_change", prompt="Refresh tomorrow's brief",
                        calendar={"connection_id": conn["id"], "calendar_id": "primary"}, local_time=None, **over)


async def _step(clock, client, scheduler, seconds):
    clock.advance(seconds=seconds)
    await scheduler.tick()


async def test_a09_baseline_then_one_run_per_relevant_change(client, clock, sessionmaker, make_scheduler, make_worker):
    conn = await connect(client)
    a = await _trigger(client, conn)
    s = make_scheduler()
    await s.tick()  # initial sync: baseline only
    got = (await client.get(f"/api/automations/{a['id']}")).json()
    assert got["trigger_status"]["initialized"] is True and (await runs(sessionmaker, a["id"]))[0] == []

    await mutate_fixture(sessionmaker, clock, conn["id"], lambda st: touch_event(st, "primary", "evdesignreview1", start={"date_time": "2026-09-28T14:00:00", "date": None, "time_zone": "America/Los_Angeles"}, end={"date_time": "2026-09-28T15:00:00", "date": None, "time_zone": "America/Los_Angeles"}))
    await _step(clock, client, s, 300)  # poll sees it; debounce holds it
    assert (await runs(sessionmaker, a["id"]))[0] == []
    await _step(clock, client, s, 61)
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and tasks[0].origin["type"] == "calendar_change"
    assert [(c["event_id"], c["kind"]) for c in tasks[0].origin["changes"]] == [("evdesignreview1", "moved")]
    assert "Design review" not in tasks[0].intent  # event text never enters the instruction

    w = make_worker(script({"tool_calls": [{"name": "automation__trigger_details", "input": {}}]}, {"text": "Brief refreshed."}))
    assert await w.run_once()
    details = json.loads((await tool_results(sessionmaker, tasks[0].id))[-1]["content"])["data"]["changes"][0]
    assert details["kind"] == "moved" and details["after"]["summary"] == {"untrusted": True, "text": "Design review"}

    # the same version delivered again through a forced full resync fires nothing
    await set_fault(sessionmaker, clock, conn["id"], "calendar.sync", "token_expired")
    await _step(clock, client, s, 300)
    await _step(clock, client, s, 61)
    # a change far outside the 48-hour window fires nothing
    def add_far(st):
        st["events"]["primary"].append({"id": "evfar", "etag": '"x"', "summary": "Offsite", "description": None, "location": None, "status": "confirmed",
                                        "start": {"date_time": "2026-10-05T10:00:00", "date": None, "time_zone": "America/Los_Angeles"},
                                        "end": {"date_time": "2026-10-05T11:00:00", "date": None, "time_zone": "America/Los_Angeles"}})
        touch_event(st, "primary", "evfar")
    await mutate_fixture(sessionmaker, clock, conn["id"], add_far)
    await _step(clock, client, s, 300)
    await _step(clock, client, s, 61)
    assert len((await runs(sessionmaker, a["id"]))[0]) == 1

    # a cancellation inside the window fires once
    await mutate_fixture(sessionmaker, clock, conn["id"], lambda st: touch_event(st, "primary", "evdentist00001", status="cancelled"))
    await _step(clock, client, s, 300)
    await _step(clock, client, s, 61)
    tasks, _ = await runs(sessionmaker, a["id"])
    assert len(tasks) == 2 and [c["kind"] for c in tasks[1].origin["changes"]] == ["cancelled"]


async def test_a09_changes_close_together_are_debounced_into_one_run(client, clock, sessionmaker, make_scheduler):
    conn = await connect(client)
    a = await _trigger(client, conn)
    s = make_scheduler()
    await s.tick()
    await mutate_fixture(sessionmaker, clock, conn["id"], lambda st: touch_event(st, "primary", "evdesignreview1", summary="Design review (v2)"))
    await _step(clock, client, s, 300)
    await mutate_fixture(sessionmaker, clock, conn["id"], lambda st: touch_event(st, "primary", "evdentist00001", location="Suite 4"))
    await _step(clock, client, s, 30)  # before the quiet period ends and before the next poll
    await _step(clock, client, s, 270)  # next poll picks up the second change
    await _step(clock, client, s, 61)
    tasks, _ = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and sorted(c["kind"] for c in tasks[0].origin["changes"]) == ["relocated", "renamed"]


async def test_a09_the_automations_own_calendar_edit_does_not_retrigger(client, clock, sessionmaker, make_scheduler, make_worker):
    conn = await connect(client, calendar="read_write")
    a = await _trigger(client, conn)
    s = make_scheduler()
    await s.tick()
    await mutate_fixture(sessionmaker, clock, conn["id"], lambda st: touch_event(st, "primary", "evdesignreview1", summary="Design review (moved)"))
    await _step(clock, client, s, 300)
    await _step(clock, client, s, 61)
    [task], _ = await runs(sessionmaker, a["id"])
    changes = {"summary": "Prep for design review", "description": None, "location": None, "start": None, "end": None}
    w = make_worker(script({"tool_calls": [call("calendar.update_event", "@auto", calendar_id="primary", event_id="evdentist00001", changes=changes)]},
                           {"text": "Updated."}))
    assert await w.run_once()
    await decide(client, await pending_approval(client))
    assert await w.run_once()
    assert (await client.get(f"/api/tasks/{task.id}")).json()["state"] == "succeeded"
    await _step(clock, client, s, 300)
    await _step(clock, client, s, 61)
    assert len((await runs(sessionmaker, a["id"]))[0]) == 1  # its own edit is recognised and ignored


async def test_a09_restored_trigger_state_does_not_refire_a_handled_change(client, clock, sessionmaker, make_scheduler, make_worker):
    """After a restore, the trigger's baseline is older than what it already acted on. The provider-event
    dedup record (one per event version) stops the same change from creating a second run."""
    conn = await connect(client)
    a = await _trigger(client, conn)
    s = make_scheduler()
    await s.tick()
    async with sessionmaker() as db:
        saved_state = dict((await db.get(Automation, a["id"])).trigger_state)  # e.g. a backup taken now
    await mutate_fixture(sessionmaker, clock, conn["id"], lambda st: touch_event(st, "primary", "evdesignreview1", summary="Design review (moved)"))
    await _step(clock, client, s, 300)
    await _step(clock, client, s, 61)
    assert len((await runs(sessionmaker, a["id"]))[0]) == 1
    assert await make_worker().run_once()  # the first run finishes, so nothing is held back by the overlap rule
    async with sessionmaker() as db, db.begin():
        (await db.get(Automation, a["id"])).trigger_state = saved_state  # restore the older state
    await _step(clock, client, s, 300)
    await _step(clock, client, s, 61)
    tasks, occs = await runs(sessionmaker, a["id"])
    assert len(tasks) == 1 and [o.state for o in occs] == ["started"]
