"""WEB-01 through the worker and API: citations, takeover (A13), data-sharing approval, stop, cancel, isolation."""
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport

from opa.api.app import create_app
from opa.browser.app import create_browser_app
from opa.browser.client import BrowserClient
from opa.worker.loop import Worker
from opa.worker.model import ScriptedAdapter
from tests.browser_fixtures import browser_service, private_server, site, web_proxy  # noqa: F401  (fixtures)
from tests.conftest import Client
from tests.helpers import call, connect, decide, pending_approval, script, task_state, tool_results


@pytest_asyncio.fixture
async def browser_client(browser_service, settings):  # noqa: F811
    bc = BrowserClient("http://browser", settings.internal_token, transport=ASGITransport(app=create_browser_app(browser_service, settings.internal_token)))
    yield bc
    await bc.aclose()


@pytest.fixture
def app(settings, clock, sessionmaker, broker_client, browser_client):
    return create_app(settings=settings, clock=clock, sessionmaker=sessionmaker, broker=broker_client, browser=browser_client)


@pytest.fixture
def make_worker(settings, sessionmaker, clock, broker_client, browser_client):
    return lambda s=None, name=None: Worker(settings, sessionmaker, ScriptedAdapter(s), clock=clock, name=name, broker=broker_client, browser=browser_client)


def nav(url, tab=None):
    return {"name": "browser__navigate", "input": {"url": url, "tab_id": tab}}


async def start(client, make_worker, steps, intent="research the Wren protocol", rid="r1"):
    conv = await client.default_conversation()
    sub = await client.submit(conv, intent, rid)
    w = make_worker(script(*steps))
    assert await w.run_once()
    return sub["task_id"], w, conv


async def test_research_cites_visited_pages(client, make_worker, sessionmaker, site, private_server):  # noqa: F811
    base = f"http://site.test:{site.port}"
    task_id, _, _ = await start(client, make_worker, [
        {"tool_calls": [nav(f"{base}/")]},
        {"tool_calls": [{"name": "browser__click_link", "input": {"ref": 1, "tab_id": None}}]},
        {"text": f"The Wren protocol was published in 2024 and has three phases. Sources: {base}/ and {base}/page2"}])
    t = await task_state(client, task_id)
    assert t["state"] == "succeeded"
    assert {k for k in t["outputs"] if k.startswith("web:")} == {f"web:{base}/", f"web:{base}/page2"}
    first, second = [json.loads(r["content"])["data"] for r in await tool_results(sessionmaker, task_id)]
    assert first["content"]["untrusted"] is True and "published in 2024" in first["content"]["text"]
    assert second["title"] == "Second page" and "three phases" in second["content"]["text"]
    [session] = (await client.get("/api/browser-sessions")).json()
    assert [u["url"] for u in session["urls_visited"]] == [f"{base}/", f"{base}/page2"]
    assert session["state"] == "stopped"  # the finished task released its browser
    assert private_server.hits == []


async def test_a13_takeover_parks_the_task_until_explicit_resume(client, make_worker, sessionmaker, site, browser_service):  # noqa: F811
    base = f"http://site.test:{site.port}"
    task_id, w, conv = await start(client, make_worker, [
        {"tool_calls": [nav(f"{base}/")]},
        {"tool_calls": [{"name": "ask_user", "input": {"question": "Keep going?"}}]},
        {"tool_calls": [nav(f"{base}/page2")]},   # attempted while the owner is in control
        {"tool_calls": [nav(f"{base}/page2")]},   # after resume
        {"text": "Done."}])
    [session] = (await client.get("/api/browser-sessions")).json()
    r = await client.post(f"/api/browser-sessions/{session['id']}/takeover")
    assert r.status_code == 200 and r.json()["control"] == "user"
    assert (await client.post(f"/api/browser-sessions/{session['id']}/input", json={"type": "wheel", "dy": 300})).status_code == 200
    shot = await client.get(f"/api/browser-sessions/{session['id']}/screenshot")
    assert shot.headers["content-type"] == "image/jpeg"
    await client.submit(conv, "yes", "r2", task_id=task_id)
    assert await w.run_once()
    t = await task_state(client, task_id)
    assert t["state"] == "waiting_user"
    paused = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])
    assert paused["error"]["code"] == "paused"
    assert await w.run_once() is False  # nothing runs while the owner has control

    r = await client.post(f"/api/browser-sessions/{session['id']}/resume")
    assert r.json()["control"] == "agent"
    assert (await task_state(client, task_id))["state"] == "queued"
    assert await browser_service.sessions[session["id"]].context.cookies() == []  # the owner's turn left nothing behind
    assert (await client.post(f"/api/browser-sessions/{session['id']}/input", json={"type": "wheel", "dy": 1})).status_code == 409
    assert await w.run_once()
    assert (await task_state(client, task_id))["state"] == "succeeded"


async def test_navigation_sharing_private_data_needs_exact_approval(client, make_worker, sessionmaker, site):  # noqa: F811
    conn = await connect(client)
    base = f"http://site.test:{site.port}"
    leaky = f"{base}/search?q=onboarding%20flow%202)%20pricing%20page"
    task_id, w, _ = await start(client, make_worker, [
        {"tool_calls": [call("gmail.get_message", conn["id"], message_id="m001")]},
        {"tool_calls": [nav(f"{base}/search?q=Wren%20protocol")]},  # no private data: no approval needed
        {"tool_calls": [nav(leaky)]},
        {"text": "Searched."}])
    assert (await task_state(client, task_id))["state"] == "waiting_approval"
    assert [h for h in site.hits if h.startswith("/search")] == ["/search?q=Wren%20protocol"]  # the leaky search has not been sent
    ap = await pending_approval(client)
    fields = {f["label"]: f["value"] for f in ap["preview"]["fields"]}
    assert ap["preview"]["title"] == "Share data with a website" and ap["account"] == "Web browser"
    assert fields["Website"] == "site.test" and fields["Full address"] == leaky and "onboarding flow 2) pricing page" in fields["Private data in the address"]
    await decide(client, ap)
    assert await w.run_once()
    assert (await task_state(client, task_id))["state"] == "succeeded"
    assert sum(h.startswith("/search?q=onboarding") for h in site.hits) == 1
    result = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])
    assert result["status"] == "succeeded" and result["data"]["url"] == leaky


async def test_denied_sharing_sends_nothing(client, make_worker, sessionmaker, site):  # noqa: F811
    conn = await connect(client)
    base = f"http://site.test:{site.port}"
    task_id, w, _ = await start(client, make_worker, [
        {"tool_calls": [call("gmail.get_message", conn["id"], message_id="m001")]},
        {"tool_calls": [nav(f"{base}/search?q=bring%20the%20metrics%3F")]},
        {"text": "ok"}])
    await decide(client, await pending_approval(client), "deny")
    await client.post(f"/api/tasks/{task_id}/resume")
    assert await w.run_once()
    assert not any(h.startswith("/search") for h in site.hits)


async def test_owner_stop_ends_browsing_for_the_task_and_cancel_closes_sessions(client, make_worker, sessionmaker, site, browser_service):  # noqa: F811
    base = f"http://site.test:{site.port}"
    task_id, w, conv = await start(client, make_worker, [
        {"tool_calls": [nav(f"{base}/")]},
        {"tool_calls": [{"name": "ask_user", "input": {"question": "Continue?"}}]},
        {"tool_calls": [nav(f"{base}/page2")]},
        {"text": "Stopped as asked."}])
    [session] = (await client.get("/api/browser-sessions")).json()
    assert (await client.post(f"/api/browser-sessions/{session['id']}/stop")).json()["state"] == "stopped"
    assert browser_service.sessions[session["id"]].stopped
    await client.submit(conv, "yes", "r2", task_id=task_id)
    assert await w.run_once()
    err = json.loads((await tool_results(sessionmaker, task_id))[-1]["content"])["error"]
    assert "owner stopped the browser" in err["message"]

    t2, w2, _ = await start(client, make_worker, [{"tool_calls": [nav(f"{base}/")]}, {"tool_calls": [{"name": "ask_user", "input": {"question": "?"}}]}],
                            intent="another", rid="r3")
    s2 = next(s for s in (await client.get("/api/browser-sessions")).json() if s["task_id"] == t2)
    await client.post(f"/api/tasks/{t2}/cancel")
    assert browser_service.sessions[s2["id"]].stopped
    assert next(s for s in (await client.get("/api/browser-sessions")).json() if s["id"] == s2["id"])["state"] == "stopped"


async def test_a14_other_owner_cannot_see_or_drive_a_browser(app, client, other_owner, make_worker, site):  # noqa: F811
    await start(client, make_worker, [{"tool_calls": [nav(f"http://site.test:{site.port}/")]}, {"tool_calls": [{"name": "ask_user", "input": {"question": "?"}}]}])
    [session] = (await client.get("/api/browser-sessions")).json()
    async with Client(transport=ASGITransport(app=app), base_url="http://test") as other:
        await other.sign_in("other@example.com", "another long password")
        assert (await other.get("/api/browser-sessions")).json() == []
        for path in ("", "/screenshot"):
            assert (await other.get(f"/api/browser-sessions/{session['id']}{path}")).status_code == 404
        for action in ("takeover", "resume", "stop"):
            assert (await other.post(f"/api/browser-sessions/{session['id']}/{action}")).status_code in (404, 409)
    assert (await client.get(f"/api/browser-sessions/{session['id']}")).json()["control"] == "agent"
