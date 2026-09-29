"""AUTO-02 through the real Google adapter against the fake Google: sync tokens, paging, deleted entries, 410."""
from datetime import timedelta

from sqlalchemy import select

from opa.db.models import Task
from opa.scheduler.service import Scheduler
from tests.acceptance.test_m3_google import broker, connect_google, fake, settings  # noqa: F401, F811  (shared pytest fixtures)


async def trigger_tasks(sm):
    async with sm() as db:
        return list(await db.scalars(select(Task).where(Task.origin.is_not(None)).order_by(Task.created_at)))


async def test_calendar_trigger_through_google_sync(client, fake, clock, sessionmaker, settings, broker_client, make_worker):  # noqa: F811
    conn, _ = await connect_google(client, fake)
    r = await client.post("/api/automations", json={"name": "Watch tomorrow", "kind": "calendar_change", "prompt": "Refresh my brief", "confirm": True,
                                                    "calendar": {"connection_id": conn["id"], "calendar_id": "primary"}})
    assert r.status_code == 201, r.text
    s = Scheduler(settings, sessionmaker, clock, broker_client)

    async def step(seconds):
        clock.advance(seconds=seconds)
        await s.tick()

    await s.tick()  # initial full sync (timeMin, paged): baseline only
    first = [q for q in fake.requests if q.url.path.endswith("/events") and q.method == "GET"]
    assert any("pageToken" in q.url.params for q in first) and all("syncToken" not in q.url.params for q in first)
    assert await trigger_tasks(sessionmaker) == []

    day = (clock.now() + timedelta(days=1)).date().isoformat()
    fake.move_event("primary", "gdesign1", f"{day}T12:00:00-07:00", f"{day}T13:00:00-07:00")
    await step(300)
    await step(61)
    [t1] = await trigger_tasks(sessionmaker)
    assert [(c["event_id"], c["kind"]) for c in t1.origin["changes"]] == [("gdesign1", "moved")]
    assert t1.origin["changes"][0]["after"]["start"] == {"date_time": f"{day}T12:00:00", "date": None, "time_zone": "America/Los_Angeles"}
    incremental = [q for q in fake.requests if "syncToken" in q.url.params]
    assert incremental and all(q.url.params["showDeleted"] == "true" for q in incremental)
    assert await make_worker().run_once()

    fake.cancel_event("primary", "gteam1")  # Google returns a minimal cancelled entry with no start time
    await step(300)
    await step(61)
    tasks = await trigger_tasks(sessionmaker)
    assert len(tasks) == 2 and [(c["event_id"], c["kind"]) for c in tasks[1].origin["changes"]] == [("gteam1", "cancelled")]
    assert await make_worker().run_once()

    fake.invalidate_sync_tokens()  # next incremental request gets 410; the broker does a full resync
    mark = len(fake.requests)
    await step(300)
    await step(61)
    assert len(await trigger_tasks(sessionmaker)) == 2  # nothing changed, so the resync fires nothing
    after = [q for q in fake.requests[mark:] if q.url.path.endswith("/events")]
    assert "syncToken" in after[0].url.params and "timeMin" in after[1].url.params  # rejected token, then a full resync
    got = (await client.get("/api/automations")).json()[0]
    assert got["trigger_status"]["last_error"] is None
