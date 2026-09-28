"""M3: Google OAuth lifecycle, Gmail and Calendar adapters (CON-01, GMAIL-01, CAL-01), A04/A05/A06/A07/A20.

Runs against tests/fake_google.py: Google's HTTP protocol, in-process, no network or real account.
Live verification against Google is intentionally not part of this suite.
"""
import asyncio
import email
import json
import logging
from email import policy
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport
from sqlalchemy import select, text

from opa.broker.service import Broker
from opa.broker.vault import generate_kek
from opa.core.settings import Settings
from opa.db.models import AuditLog, BrokerCredential, Connection, OAuthState
from opa.sdk.contract import ToolRequest
from tests.conftest import Client
from tests.fake_google import CAL_READ, CAL_WRITE, GMAIL_READ, GMAIL_SEND, FakeGoogle
from tests.helpers import action_of, call, decide, pending_approval, script, send_call, task_state, tool_results, when

KEK = generate_kek()


@pytest.fixture
def settings(pg_url):
    return Settings(database_url=pg_url, mode="live", model_provider="scripted", internal_token="test-internal-token",
                    tool_timeout_seconds=5, broker_kek=KEK, google_client_id="test-client", google_client_secret="test-secret")


@pytest.fixture
def fake(clock):
    return FakeGoogle(clock)


@pytest.fixture
def broker(settings, sessionmaker, clock, fake):
    return Broker(settings, sessionmaker, clock, read_retry_base=0, google_transport=fake.transport)


async def authorize(client, modes=None, connection_id=None):
    r = await client.post("/api/connectors/google/authorize", json={"modes": modes or {"gmail": "read_only", "calendar": "read_only"}, "connection_id": connection_id})
    assert r.status_code == 200, r.text
    return r.json()


async def connect_google(client, fake, modes=None, sub="sub-1", grant=None, connection_id=None):
    auth = await authorize(client, modes, connection_id)
    r = await client.get(fake.consent(auth["authorization_url"], sub=sub, grant=grant))
    assert r.status_code == 303, r.text
    loc = parse_qs(urlparse(r.headers["location"]).query)
    assert loc["oauth"] == ["connected"], loc
    conns = (await client.get("/api/connections")).json()
    return next(c for c in conns if c["id"] == loc["connection"][0]), loc


async def run_task(client, make_worker, steps, intent="do it", rid="r1"):
    conv = await client.default_conversation()
    sub = await client.submit(conv, intent, rid)
    w = make_worker(script(*steps))
    assert await w.run_once()
    return sub["task_id"], w


# ------------------------------------------------------------------ OAuth lifecycle

async def test_authorize_url_and_connect(client, fake, sessionmaker):
    auth = await authorize(client)
    q = {k: v[0] for k, v in parse_qs(urlparse(auth["authorization_url"]).query).items()}
    assert auth["authorization_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert set(q["scope"].split()) == {"openid", "email", GMAIL_READ, CAL_READ}  # read scopes only at first connect
    assert q["redirect_uri"] == "http://127.0.0.1:8080/api/oauth/google/callback"
    assert (q["access_type"], q["prompt"], q["include_granted_scopes"], q["code_challenge_method"]) == ("offline", "consent", "true", "S256")

    conn, _ = await connect_google(client, fake)
    assert (conn["provider"], conn["account_label"], conn["status"]) == ("google", "owner@gmail.example", "connected")
    assert conn["modes"] == {"gmail": "read_only", "calendar": "read_only"}
    assert set(conn["granted_scopes"]) == {"openid", "email", GMAIL_READ, CAL_READ}
    async with sessionmaker() as db:
        cred = await db.scalar(select(BrokerCredential).where(BrokerCredential.connection_id == conn["id"]))
        assert cred is not None and all(t.encode() not in cred.secret_enc for t in fake.issued)
        states = list(await db.scalars(select(OAuthState).order_by(OAuthState.created_at)))
        assert [s.used_at is not None for s in states] == [False, True]  # the first request was never completed
        assert states[1].code_verifier == "used"


@pytest.mark.parametrize("case", ["unknown_state", "replay", "other_session", "expired", "denied"])
async def test_callback_rejects_bad_state(case, app, client, fake, clock, sessionmaker):
    auth = await authorize(client)
    cb = fake.consent(auth["authorization_url"], deny=case == "denied")
    if case == "unknown_state":
        cb = cb.replace("state=", "state=x")
    if case == "replay":
        assert (await client.get(cb)).status_code == 303
    if case == "expired":
        clock.advance(minutes=11)
    if case == "other_session":
        async with Client(transport=ASGITransport(app=app), base_url="http://test") as second:
            await second.sign_in()  # same owner, different browser session
            r = await second.get(cb)
    else:
        r = await client.get(cb)
    loc = parse_qs(urlparse(r.headers["location"]).query)
    assert loc["oauth"] == ["error"] and loc["reason"][0]
    async with sessionmaker() as db:
        expected = 1 if case == "replay" else 0
        assert len(list(await db.scalars(select(Connection)))) == expected


async def test_incremental_scope_for_writes(client, fake):
    conn, _ = await connect_google(client, fake)
    r = await client.patch(f"/api/connections/{conn['id']}", json={"modes": {"gmail": "read_write"}, "expected_version": conn["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "scope_required" and GMAIL_SEND in r.json()["error"]["message"]
    auth = await authorize(client, {"gmail": "read_write"}, conn["id"])
    assert set(auth["scopes"]) == {"openid", "email", GMAIL_READ, GMAIL_SEND}
    again, _ = await connect_google(client, fake, {"gmail": "read_write"}, connection_id=conn["id"])
    assert again["id"] == conn["id"]
    assert again["modes"] == {"gmail": "read_write", "calendar": "read_only"}  # calendar untouched
    assert {GMAIL_SEND, CAL_READ} <= set(again["granted_scopes"])  # earlier grants are kept


async def test_granular_consent_limits_the_mode(client, fake):
    conn, loc = await connect_google(client, fake, {"gmail": "read_write", "calendar": "read_write"}, grant=[GMAIL_READ, CAL_READ, CAL_WRITE])
    assert conn["modes"] == {"gmail": "read_only", "calendar": "read_write"}
    assert "gmail" in loc["note"][0]


async def test_same_account_reuses_connection_other_account_is_separate(client, fake):
    first, _ = await connect_google(client, fake)
    again, _ = await connect_google(client, fake)
    assert again["id"] == first["id"]
    other, loc = await connect_google(client, fake, sub="sub-2", connection_id=first["id"])
    assert other["id"] != first["id"] and other["account_label"] == "second@gmail.example"
    assert "different account" in loc["note"][0]
    assert len((await client.get("/api/connections")).json()) == 2


# ------------------------------------------------------------------ reads

async def test_gmail_reads_normalize_filter_and_mark_untrusted(client, fake, make_worker, sessionmaker):
    conn, _ = await connect_google(client, fake)
    steps = [{"tool_calls": [call("gmail.search_messages", conn["id"], query="", max_results=10, page_token=None)]},
             {"tool_calls": [call("gmail.get_message", conn["id"], message_id="g2")]},
             {"tool_calls": [call("gmail.get_thread", conn["id"], thread_id="th1")]},
             {"text": "done"}]
    task_id, _ = await run_task(client, make_worker, steps)
    search, html_msg, thread = [json.loads(r["content"]) for r in await tool_results(sessionmaker, task_id)]
    assert search["status"] == "succeeded", search
    assert search["data"]["filtered_count"] == 1 and {m["id"] for m in search["data"]["messages"]} == {"g1", "g2", "g4"}
    body = html_msg["data"]["message"]["body"]
    assert body["untrusted"] is True and "Hello there" in body["text"] and "(https://shop.example/d)" in body["text"]
    assert "x()" not in body["text"] and "p{}" not in body["text"]
    assert [m["id"] for m in thread["data"]["messages"]] == ["g1", "g4"] or {m["id"] for m in thread["data"]["messages"]} == {"g1", "g4"}
    assert "771204" not in json.dumps([search, html_msg, thread])


async def test_calendar_reads_normalize_local_time(client, fake, make_worker, sessionmaker, clock):
    conn, _ = await connect_google(client, fake)
    steps = [{"tool_calls": [call("calendar.list_calendars", conn["id"])]},
             {"tool_calls": [call("calendar.list_events", conn["id"], calendar_id="primary", time_min="2026-09-28T00:00:00-07:00", time_max="2026-09-29T00:00:00-07:00", page_token=None)]},
             {"text": "done"}]
    task_id, _ = await run_task(client, make_worker, steps)
    cals, events = [json.loads(r["content"])["data"] for r in await tool_results(sessionmaker, task_id)]
    assert cals["calendars"][0] == {"id": "primary", "summary": "owner@gmail.example", "time_zone": "America/Los_Angeles", "primary": True}
    design = next(e for e in events["events"] if e["id"] == "gdesign1")
    assert design["start"] == when("2026-09-28T10:00:00")
    assert next(e for e in events["events"] if e["id"] == "gholiday")["start"]["date"] == "2026-09-28"
    assert next(e for e in events["events"] if e["id"] == "gweekly_1")["recurringEventId"] == "gweekly"


# ------------------------------------------------------------------ A07 token lifecycle

async def _claimed(client, make_worker):
    conv = await client.default_conversation()
    await client.submit(conv, "probe", "probe-1")
    task_id, _, token = await make_worker().claim()
    return task_id, token


async def test_a07_expired_token_refreshes_once_for_concurrent_calls(client, fake, make_worker, broker_client, clock):
    conn, _ = await connect_google(client, fake)
    clock.advance(hours=2)  # the access token (1 h) is now expired
    task_id, token = await _claimed(client, make_worker)
    req = ToolRequest(task_id=task_id, action_id="a", connection_id=conn["id"], tool_name="calendar.list_calendars", tool_version="1", arguments={})
    results = await asyncio.gather(*[broker_client.read(req, token) for _ in range(4)])
    assert all(r.status == "succeeded" for r in results), results
    assert fake.refresh_calls == 1


async def test_401_triggers_one_refresh_and_retry(client, fake, make_worker, broker_client):
    conn, _ = await connect_google(client, fake)
    task_id, token = await _claimed(client, make_worker)
    fake.invalidate_access_tokens()
    r = await broker_client.read(ToolRequest(task_id=task_id, action_id="a", connection_id=conn["id"], tool_name="calendar.list_calendars", tool_version="1", arguments={}), token)
    assert r.status == "succeeded" and fake.refresh_calls == 1


async def test_a07_revoked_grant_requires_reauth_and_leaks_no_secrets(client, fake, make_worker, sessionmaker, clock, caplog):
    caplog.set_level(logging.DEBUG)
    conn, _ = await connect_google(client, fake)
    fake.revoke_user("sub-1")
    clock.advance(hours=2)
    task_id, _ = await run_task(client, make_worker, [{"tool_calls": [call("calendar.list_calendars", conn["id"])]}, {"text": "done"}])
    err = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])["error"]
    assert err["code"] == "auth_required"
    c = next(x for x in (await client.get("/api/connections")).json() if x["id"] == conn["id"])
    assert c["status"] == "reauth_required"
    notes = (await client.get("/api/notifications")).json()
    assert any(n["title"] == "Reconnect your Google account" and "owner@gmail.example" in n["body"] for n in notes)

    # no secret appears in logs, API output, events, notifications, audit rows, or checkpoints
    async with sessionmaker() as db:
        audit = json.dumps([a.sanitized for a in await db.scalars(select(AuditLog))])
        checkpoints = json.dumps([r[0] for r in (await db.execute(text("SELECT checkpoint FROM tasks"))).all()])
        states = json.dumps([r for r in (await db.execute(text("SELECT code_verifier, scopes FROM oauth_states"))).all()], default=str)
    surfaces = [caplog.text, audit, checkpoints, states, json.dumps((await client.get("/api/connections")).json()),
                json.dumps((await client.get("/api/events/replay")).json()), json.dumps(notes)]
    assert fake.issued and not any(secret in s for secret in fake.issued for s in surfaces)

    again, _ = await connect_google(client, fake, connection_id=conn["id"])
    assert again["id"] == conn["id"] and again["status"] == "connected"


async def test_health_check(client, fake, clock):
    conn, _ = await connect_google(client, fake)
    r = await client.post(f"/api/connections/{conn['id']}/health")
    assert r.json() == {"status": "connected", "detail": None}
    assert (await client.get("/api/connections")).json()[0]["last_health_at"]
    fake.revoke_user("sub-1")
    clock.advance(hours=2)
    assert (await client.post(f"/api/connections/{conn['id']}/health")).json()["status"] == "reauth_required"


async def test_testing_mode_expiry_warning(client, fake, clock):
    await connect_google(client, fake)
    assert (await client.get("/api/connections")).json()[0]["warning"] is None
    clock.advance(days=6, minutes=1)
    assert "7 days" in (await client.get("/api/connections")).json()[0]["warning"]


# ------------------------------------------------------------------ writes

async def _approve_and_run(client, w):
    await decide(client, await pending_approval(client))
    assert await w.run_once()


async def test_send_builds_exact_message(client, fake, make_worker, sessionmaker):
    conn, _ = await connect_google(client, fake, {"gmail": "read_write", "calendar": "off"})
    task_id, w = await run_task(client, make_worker, [{"tool_calls": [send_call(conn["id"], cc=["cc@partner.example"], bcc=["hidden@partner.example"])]}, {"text": "Sent."}])
    await _approve_and_run(client, w)
    assert (await task_state(client, task_id))["state"] == "succeeded"
    [sent] = fake.sent()
    msg = email.message_from_bytes(sent["raw"], policy=policy.default)
    action = await action_of(sessionmaker, task_id)
    assert (msg["From"], msg["To"], msg["Cc"], msg["Subject"]) == ("owner@gmail.example", "priya@partner.example", "cc@partner.example", "Metrics for Thursday")
    assert msg["Message-ID"] == action.canonical_args["message_id_header"]
    assert sent["bcc"] == "hidden@partner.example"
    assert msg.get_content().strip() == "Hi Priya, the metrics are attached below.\n42"
    assert action.provider_refs == [f"gmail:{sent['id']}"]


async def test_reply_threads_with_original(client, fake, make_worker):
    conn, _ = await connect_google(client, fake, {"gmail": "read_write", "calendar": "off"})
    _, w = await run_task(client, make_worker, [{"tool_calls": [send_call(conn["id"], subject="Re: Agenda for Thursday", in_reply_to_message_id="g1")]}, {"text": "Replied."}])
    await _approve_and_run(client, w)
    [sent] = fake.sent()
    msg = email.message_from_bytes(sent["raw"], policy=policy.default)
    assert sent["threadId"] == "th1" and msg["In-Reply-To"] == "<a1@partner.example>" and msg["References"] == "<a1@partner.example>"


@pytest.mark.parametrize("kind, state", [("500_after", "unknown"), ("timeout_after", "unknown"), ("connect_error", "failed")])
async def test_a04_send_failure_classification(kind, state, client, fake, make_worker, sessionmaker):
    conn, _ = await connect_google(client, fake, {"gmail": "read_write", "calendar": "off"})
    fake.fault("send", kind)
    task_id, w = await run_task(client, make_worker, [{"tool_calls": [send_call(conn["id"])]}, {"text": "Reported."}])
    await _approve_and_run(client, w)
    action = await action_of(sessionmaker, task_id)
    assert action.state == state
    if state == "unknown":
        assert len(fake.sent()) == 1
        r = await client.post(f"/api/actions/{action.id}/reconcile")
        assert r.json()["state"] == "succeeded"
        await client.post(f"/api/tasks/{task_id}/resume")
        assert await w.run_once()
        assert (await task_state(client, task_id))["state"] == "succeeded"
    else:
        assert fake.sent() == [] and action.error["code"] == "provider_unavailable"
    assert len(fake.sent()) == (1 if state == "unknown" else 0)  # never resent


def _create(conn_id):
    return call("calendar.create_event", conn_id, calendar_id="primary", summary="Focus", description=None, location="Desk",
                start=when("2026-09-29T09:00:00"), end=when("2026-09-29T10:30:00"), confirm_past=False)


async def test_a05_create_event_recovers_by_client_id(client, fake, make_worker, sessionmaker):
    conn, _ = await connect_google(client, fake, {"gmail": "off", "calendar": "read_write"})
    fake.fault("insert", "timeout_after")
    task_id, w = await run_task(client, make_worker, [{"tool_calls": [_create(conn["id"])]}, {"text": "Created."}])
    await _approve_and_run(client, w)
    action = await action_of(sessionmaker, task_id)
    assert action.state == "unknown"
    assert (await client.post(f"/api/actions/{action.id}/reconcile")).json()["state"] == "succeeded"
    ev = fake.events["primary"][action.provider_idempotency_key]
    assert ev["start"] == {"dateTime": "2026-09-29T09:00:00", "timeZone": "America/Los_Angeles"} and ev["location"] == "Desk"
    assert "attendees" not in ev
    inserts = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/events")]
    assert len(inserts) == 1 and inserts[0].url.params["sendUpdates"] == "none"


async def test_update_sends_if_match_and_rejects_stale(client, fake, make_worker, sessionmaker):
    conn, _ = await connect_google(client, fake, {"gmail": "off", "calendar": "read_write"})
    changes = {"summary": "Design review (moved)", "description": None, "location": None, "start": None, "end": None}
    task_id, w = await run_task(client, make_worker, [{"tool_calls": [call("calendar.update_event", conn["id"], calendar_id="primary", event_id="gdesign1", changes=changes)]}, {"text": "ok"}])
    ap = await pending_approval(client)
    before = next(f for f in ap["preview"]["fields"] if f["label"] == "Summary")["before"]
    assert before == "Design review"
    fake.events["primary"]["gdesign1"]["etag"] = '"changed-elsewhere"'
    await _approve_and_run(client, w)
    action = await action_of(sessionmaker, task_id)
    assert action.state == "failed" and action.error["code"] == "conflict"
    assert fake.events["primary"]["gdesign1"]["summary"] == "Design review"
    patch = next(r for r in fake.requests if r.method == "PATCH")
    assert patch.headers["if-match"] == action.canonical_args["expected_etag"]


# ------------------------------------------------------------------ errors, scopes, disconnect, live/demo

async def test_rate_limit_is_retried_for_reads(client, fake, make_worker, sessionmaker):
    conn, _ = await connect_google(client, fake)
    fake.fault("calendarList", "429")
    task_id, _ = await run_task(client, make_worker, [{"tool_calls": [call("calendar.list_calendars", conn["id"])]}, {"text": "ok"}])
    assert json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])["status"] == "succeeded"


async def test_missing_google_scope_hides_tools_and_broker_refuses(client, fake, make_worker, broker_client, sessionmaker):
    conn, _ = await connect_google(client, fake)
    async with sessionmaker() as db, db.begin():  # e.g. the owner unticked Calendar on the consent screen
        c = await db.get(Connection, conn["id"])
        c.granted_scopes = [s for s in c.granted_scopes if s != CAL_READ]
    task_id, token = await _claimed(client, make_worker)
    names = [t["name"] for t in await broker_client.tools(task_id, token)]
    assert "gmail__search_messages" in names and "calendar__list_calendars" not in names
    r = await broker_client.read(ToolRequest(task_id=task_id, action_id="a", connection_id=conn["id"], tool_name="calendar.list_calendars", tool_version="1", arguments={}), token)
    assert r.error.code == "permission_denied" and "has not granted" in r.error.message


@pytest.mark.parametrize("revoke_fails", [False, True])
async def test_a06_disconnect_revokes_and_clears_credentials(revoke_fails, client, fake, sessionmaker):
    conn, _ = await connect_google(client, fake)
    if revoke_fails:
        fake.fault("revoke", "503")
    r = await client.delete(f"/api/connections/{conn['id']}")
    body = r.json()
    assert body["status"] == "disconnected" and body["revoked"] is (not revoke_fails)
    assert (body["revocation_error"] is not None) is revoke_fails
    assert (len(fake.revoked) == 1) is (not revoke_fails)
    async with sessionmaker() as db:
        assert await db.scalar(select(BrokerCredential).where(BrokerCredential.connection_id == conn["id"])) is None
    notes = (await client.get("/api/notifications")).json()
    assert ("revocation failed" in notes[0]["body"]) is revoke_fails
    assert (await client.get("/api/connections")).json() == []


async def test_a20_live_mode_has_no_fixture_and_reports_missing_google_config(client, app, settings):
    kinds = {c["provider"]: c for c in (await client.get("/api/connectors")).json()}
    assert "fixture" not in kinds and kinds["google"]["available"] is True
    assert (await client.post("/api/connections", json={"provider": "fixture"})).status_code == 422
    app.state.settings = settings.model_copy(update={"google_client_id": None})
    google = (await client.get("/api/connectors")).json()[0]
    assert google["available"] is False and "OPA_GOOGLE_CLIENT_ID" in google["reason"]
    r = await client.post("/api/connectors/google/authorize", json={"modes": {"gmail": "read_only"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_configured"
