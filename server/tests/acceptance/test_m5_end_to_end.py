"""Spec section 12, run end to end on the fixture provider with the scripted model.

"Every weekday at 6 PM, prepare tomorrow's meeting brief from my calendar, relevant email, and the files
I uploaded. Update it if tomorrow's meetings change. Ask before emailing it to me."
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select

from opa.broker.fixture import touch_event
from opa.db.models import Action, Occurrence, Task
from opa.scheduler.service import Scheduler
from tests.helpers import call, connect, decide, fixture_state, mutate_fixture, pending_approval, script, sent_messages, tool_results

LA = "America/Los_Angeles"
ME = "demo@example.test"


def at(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


async def automation_tasks(sm, automation_id):
    async with sm() as db:
        return list(await db.scalars(select(Task).where(Task.origin["automation_id"].astext == automation_id).order_by(Task.created_at)))


async def test_weekday_brief_with_change_trigger_and_approved_email(client, clock, sessionmaker, settings, broker_client, make_worker):
    clock.set(at(2026, 9, 29, 0, 0))  # Monday 17:00 PDT; tomorrow's meetings are on Tuesday the 29th
    await client.sign_in()
    await client.patch("/api/me", json={"time_zone": LA})
    conn = await connect(client, gmail="read_write", calendar="read_only")
    up = await client.post("/api/artifacts?filename=prep-notes.md", content=b"# Prep notes\n\nAsk Priya about Q4 pricing.\n",
                           headers={"content-type": "text/markdown"})
    assert up.status_code == 201, up.text
    notes = up.json()

    # Step 1: the owner reviews the normalized schedule, then confirms it. Weekdays = five weekly days, one automation.
    brief_prompt = f"Prepare tomorrow's meeting brief from my calendar, relevant email, and the files I uploaded. Ask before emailing it to me at {ME}."
    spec = {"name": "Weekday meeting brief", "kind": "weekly", "prompt": brief_prompt, "time_zone": LA, "local_time": "18:00",
            "weekdays": ["mon", "tue", "wed", "thu", "fri"], "connection_ids": [conn["id"]]}
    preview = (await client.post("/api/automations/preview", json=spec)).json()
    assert preview["description"] == "Every weekday at 18:00 (America/Los_Angeles)" and preview["upcoming"][0]["local"] == "Mon 2026-09-28 18:00 PDT"
    brief = (await client.post("/api/automations", json=spec | {"confirm": True})).json()
    watch = (await client.post("/api/automations", json={
        "name": "Refresh brief when tomorrow changes", "kind": "calendar_change", "confirm": True, "connection_ids": [conn["id"]],
        "prompt": f"Tomorrow's meetings changed. Update the meeting brief and ask before emailing the update to me at {ME}.",
        "calendar": {"connection_id": conn["id"], "calendar_id": "primary"}})).json()

    s = Scheduler(settings, sessionmaker, clock, broker_client)
    await s.tick()  # Step 2: the trigger records a baseline and alerts nothing
    assert await automation_tasks(sessionmaker, watch["id"]) == []

    clock.set(at(2026, 9, 29, 1, 0, 5))  # Monday 18:00:05 PDT
    await s.tick()
    [run] = await automation_tasks(sessionmaker, brief["id"])
    assert run.allowed_connection_ids == [conn["id"]]

    # Step 3: read only the agreed sources, write a sourced brief, render it, and propose the email.
    content = ("# Meeting brief: Tuesday 2026-09-29\n\n"
               "- Design review at 10:00 (calendar:primary/evdesignreview1). Priya asked for the metrics (gmail:m001).\n"
               "- Prep: ask Priya about Q4 pricing (from prep-notes.md).\n\n"
               "Unknown: the agenda for the Team sync is not in any source.\n")
    w = make_worker(script(
        {"tool_calls": [call("calendar.list_events", "@auto", calendar_id="primary", time_min="2026-09-29T00:00:00-07:00", time_max="2026-09-30T00:00:00-07:00", page_token=None)]},
        {"tool_calls": [call("gmail.search_messages", "@auto", query="from:priya", max_results=10, page_token=None)]},
        {"tool_calls": [{"name": "artifact__read", "input": {"artifact_id": notes["id"], "version": None}}]},
        {"tool_calls": [{"name": "artifact__create_markdown", "input": {"name": "Meeting brief 2026-09-29", "content": content,
                                                                        "source_refs": ["calendar:primary/evdesignreview1", "gmail:m001", f"artifact:{notes['id']}@v1"]}}]},
        {"tool_calls": [{"name": "artifact__render_pdf", "input": {"artifact_id": "@last.artifact_id", "version": None}}]},
        {"tool_calls": [call("gmail.send_message", "@auto", to=[ME], cc=[], bcc=[], subject="Meeting brief for Tuesday", body="Your brief is attached.",
                             in_reply_to_message_id=None, attachments=[{"artifact_id": "@last.artifact_id", "version": "@last.version"}])]},
        {"text": "Your brief for Tuesday was emailed to you."}))
    assert await w.run_once()
    reads = [json.loads(r["content"]) for r in await tool_results(sessionmaker, run.id)]
    assert [r["status"] for r in reads[:5]] == ["succeeded"] * 5, reads
    pdf = reads[4]["data"]
    assert (await client.get(f"/api/tasks/{run.id}")).json()["state"] == "waiting_approval"
    assert await sent_messages(sessionmaker, clock, conn["id"]) == []  # a scheduled run never sends on its own

    # Step 5: the owner approves the exact email with the current artifact version attached.
    ap = await pending_approval(client)
    fields = {f["label"]: f["value"] for f in ap["preview"]["fields"]}
    assert fields["To"] == ME and f"version {pdf['version']}" in fields["Attachments"] and pdf["sha256"][:12] in fields["Attachments"]
    await decide(client, ap)
    assert await w.run_once()
    [sent] = await sent_messages(sessionmaker, clock, conn["id"])
    assert sent["to"] == ME and sent["attachments"][0]["sha256"] == pdf["sha256"]
    async with sessionmaker() as db:
        action = (await db.scalars(select(Action).where(Action.task_id == run.id))).one()
    assert action.state == "succeeded" and action.provider_refs == [f"gmail:{sent['id']}"]  # Step 7: provider reference
    assert (await client.get(f"/api/tasks/{run.id}")).json()["state"] == "succeeded"

    # Step 4: tomorrow's design review moves; exactly one revision run follows.
    await mutate_fixture(sessionmaker, clock, conn["id"], lambda st: touch_event(
        st, "primary", "evdesignreview1", start={"date_time": "2026-09-29T13:30:00", "date": None, "time_zone": LA},
        end={"date_time": "2026-09-29T14:30:00", "date": None, "time_zone": LA}))
    clock.set(at(2026, 9, 29, 1, 5, 10))
    await s.tick()
    clock.set(at(2026, 9, 29, 1, 6, 20))
    await s.tick()
    [revision] = await automation_tasks(sessionmaker, watch["id"])
    assert [(c["event_id"], c["kind"]) for c in revision.origin["changes"]] == [("evdesignreview1", "moved")]
    md_id = reads[3]["data"]["artifact_id"]
    w2 = make_worker(script(
        {"tool_calls": [{"name": "automation__trigger_details", "input": {}}]},
        {"tool_calls": [{"name": "artifact__edit_markdown", "input": {"artifact_id": md_id, "expected_version": 1,
                                                                      "edits": [{"old_text": "Design review at 10:00", "new_text": "Design review at 13:30 (moved from 10:00)"}]}}]},
        {"tool_calls": [{"name": "artifact__render_pdf", "input": {"artifact_id": md_id, "version": None}}]},
        {"tool_calls": [call("gmail.send_message", "@auto", to=[ME], cc=[], bcc=[], subject="Updated meeting brief for Tuesday",
                             body="The design review moved to 13:30. The updated brief is attached.", in_reply_to_message_id=None,
                             attachments=[{"artifact_id": "@last.artifact_id", "version": "@last.version"}])]},
        {"text": "The brief was updated and emailed."}))
    assert await w2.run_once()
    rev_pdf = json.loads((await tool_results(sessionmaker, revision.id))[-1]["content"])["data"]
    assert rev_pdf["artifact_id"] == pdf["artifact_id"] and rev_pdf["version"] == pdf["version"] + 1  # same PDF, new version
    await decide(client, await pending_approval(client))  # Step 6: the update is a new proposal, never sent automatically
    assert await w2.run_once()
    sent = await sent_messages(sessionmaker, clock, conn["id"])
    assert len(sent) == 2 and sent[1]["attachments"][0]["sha256"] == rev_pdf["sha256"]

    # No alert loop: later polls see the same event version and create nothing.
    for minute in (12, 20, 30):
        clock.set(at(2026, 9, 29, 1, minute, 0))
        await s.tick()
    assert len(await automation_tasks(sessionmaker, watch["id"])) == 1
    async with sessionmaker() as db:
        states = [o.state for o in await db.scalars(select(Occurrence).where(Occurrence.automation_id == brief["id"]))]
    assert states == ["started"]
    state = await fixture_state(sessionmaker, clock, conn["id"])
    assert next(e for e in state["events"]["primary"] if e["id"] == "evdesignreview1")["start"]["date_time"] == "2026-09-29T13:30:00"
