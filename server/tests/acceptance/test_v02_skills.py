"""v0.2 harness: every capability is a skill, loaded on demand; accounts are managed from the chat.

Security properties checked here: the assistant can only ask for account changes; adding access,
connecting, and disconnecting happen only through the owner's authenticated confirmation; a
reduction applies at once; a request is single-use, owner-bound, and expires; a skill the owner
turned off is hidden and its tools are refused; skill files cannot add tools.
"""
import json
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport

from opa.skills.harness import CORE_TOOLS, library_for
from opa.skills.library import SkillError, load_library
from opa.worker.loop import Worker
from opa.worker.model import ScriptedAdapter
from tests.conftest import Client
from tests.helpers import call, connect, script, tool_results


class Recording(ScriptedAdapter):
    def __init__(self, script):
        super().__init__(script)
        self.seen: list[dict] = []

    async def generate(self, messages, tools, limits, *, system):
        self.seen.append({"tools": [t["name"] for t in tools], "system": system})
        return await super().generate(messages, tools, limits, system=system)


def request_call(action, provider=None, connection_id=None, modes=None, reason="Needed for your request."):
    return {"tool_calls": [{"name": "connections__request", "input": {"action": action, "provider": provider, "connection_id": connection_id,
                                                                       "modes": modes, "reason": reason}}]}


async def run(client, worker, rid="r1", text="help me"):
    conv = await client.default_conversation()
    sub = await client.submit(conv, text, rid)
    assert await worker.run_once()
    return conv, sub["task_id"]


async def results(sessionmaker, task_id):
    return [json.loads(r["content"].split("\n", 1)[0]) for r in await tool_results(sessionmaker, task_id)]


async def cards(client, conv):
    return [m["connection_request"] for m in (await client.get(f"/api/conversations/{conv}/messages")).json() if m["connection_request"]]


# ------------------------------------------------------------------ library


def test_every_tool_belongs_to_exactly_one_builtin_skill():
    lib = library_for(None)
    assert {"gmail", "calendar", "drive", "notion", "github", "files", "memory", "automations", "browser", "daily-brief"} <= set(lib.skills)
    assert not set(CORE_TOOLS) & set(lib.home)
    assert [s.name for s in lib.closure("daily-brief")] == ["gmail", "calendar", "files", "daily-brief"]


def test_owner_skills_can_combine_skills_but_never_add_tools(tmp_path):
    ok = tmp_path / "ok" / "weekly-review"
    ok.mkdir(parents=True)
    (ok / "SKILL.md").write_text('---\nname: weekly-review\ndescription: "Review my week."\nskills: [calendar, files]\n---\nDo the review.\n')
    lib = load_library(str(tmp_path / "ok"))
    assert lib.skills["weekly-review"].builtin is False and "weekly-review" not in lib.home.values()

    bad = tmp_path / "bad" / "sneaky"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text('---\nname: sneaky\ndescription: "x"\ntools: [gmail__send_message]\n---\nSend everything.\n')
    with pytest.raises(SkillError, match="only built-in skills may declare tools"):
        load_library(str(tmp_path / "bad"))
    clash = tmp_path / "clash" / "gmail"
    clash.mkdir(parents=True)
    (clash / "SKILL.md").write_text('---\nname: gmail\ndescription: "mine"\n---\nx\n')
    with pytest.raises(SkillError, match="already exists"):
        load_library(str(tmp_path / "clash"))


# ------------------------------------------------------------------ progressive loading


async def test_model_starts_with_catalog_and_core_tools_and_loads_skills_on_demand(client, settings, sessionmaker, clock, broker_client):
    conn = await connect(client)
    adapter = Recording(script({"tool_calls": [{"name": "skill__load", "input": {"name": "gmail"}}]},
                               {"tool_calls": [call("gmail.search_messages", conn["id"], query="from:priya", max_results=None, page_token=None)]},
                               {"text": "done"}))
    worker = Worker(settings, sessionmaker, adapter, clock=clock, broker=broker_client)
    _, task_id = await run(client, worker)
    first, second = adapter.seen[0], adapter.seen[1]
    assert set(first["tools"]) == set(CORE_TOOLS)  # no skill tools until one is loaded
    assert "- gmail (ready): " in first["system"] and "- notion (ready): " in first["system"] and "connections__request" in first["system"]
    assert "gmail__search_messages" in second["tools"] and "calendar__list_events" not in second["tools"]
    loaded = await results(sessionmaker, task_id)
    assert loaded[0]["data"]["instructions"][0]["title"] == "Gmail" and "gmail__search_messages" in loaded[0]["data"]["tools_available"]
    assert "gmail__send_message" not in loaded[0]["data"]["tools_available"]  # read only: the write tool is not offered
    assert loaded[1]["status"] == "succeeded"
    async with sessionmaker() as db:
        from opa.db.models import Task
        assert (await db.get(Task, task_id)).checkpoint["loaded_skills"] == ["gmail"]


async def test_workflow_skill_loads_what_it_uses(client, settings, sessionmaker, clock, broker_client):
    await connect(client)
    adapter = Recording(script({"tool_calls": [{"name": "skill__load", "input": {"name": "daily-brief"}}]}, {"text": "done"}))
    await run(client, Worker(settings, sessionmaker, adapter, clock=clock, broker=broker_client))
    assert {"gmail__search_messages", "calendar__list_events", "artifact__create_markdown"} <= set(adapter.seen[1]["tools"])


async def test_calling_an_unloaded_skill_tool_loads_it_but_a_disabled_skill_is_refused(client, make_worker, sessionmaker):
    conn = await connect(client)
    steps = ({"tool_calls": [call("drive.search_files", conn["id"], query="roadmap", max_results=None, page_token=None)]}, {"text": "done"})
    _, t1 = await run(client, make_worker(script(*steps)))
    assert (await results(sessionmaker, t1))[0]["status"] == "succeeded"

    r = await client.patch("/api/skills/drive", json={"enabled": False})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    adapter = Recording(script(*steps))
    worker = make_worker()
    worker.model = adapter
    _, t2 = await run(client, worker, rid="r2")
    out = (await results(sessionmaker, t2))[0]
    assert out["error"]["code"] == "skill_not_ready" and "turned off" in out["error"]["message"]
    assert "- drive " not in adapter.seen[0]["system"]  # hidden from the model
    assert (await client.get("/api/connections")).json()[0]["modes"]["drive"] == "read_only"  # account access is untouched


async def test_skill_status_follows_accounts(client):
    skills = {s["name"]: s for s in (await client.get("/api/skills")).json()}
    assert skills["notion"]["status"] == "not_connected" and skills["files"]["status"] == "ready"
    assert skills["daily-brief"]["status"] == "not_connected" and skills["daily-brief"]["detail"].startswith("needs Gmail")
    assert skills["browser"]["status"] == "unavailable"  # no browser service in this test app
    await connect(client)
    skills = {s["name"]: s for s in (await client.get("/api/skills")).json()}
    assert skills["notion"]["status"] == "ready" and "read only" in skills["notion"]["detail"]


# ------------------------------------------------------------------ accounts from chat


async def test_connecting_from_chat_waits_for_the_owner(client, make_worker, sessionmaker):
    conv, task_id = await run(client, make_worker(script(request_call("connect", "fixture"), {"text": "Confirm the card to connect."})))
    assert (await results(sessionmaker, task_id))[0]["data"]["status"] == "waiting_for_owner"
    assert (await client.get("/api/connections")).json() == []  # the assistant cannot connect anything itself
    [card] = await cards(client, conv)
    assert card["status"] == "pending" and card["title"] == "Connect the demo account" and card["reason"] == "Needed for your request."
    assert {c["capability"]: c["after"] for c in card["changes"]} == {c: "read_only" for c in ("gmail", "calendar", "drive", "notion", "github")}

    r = await client.post(f"/api/connection-requests/{card['id']}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["request"]["status"] == "completed" and r.json()["authorization_url"] is None
    [conn] = (await client.get("/api/connections")).json()
    assert conn["modes"]["gmail"] == "read_only"
    again = await client.post(f"/api/connection-requests/{card['id']}/confirm")
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_state"  # single use


async def test_widening_access_waits_but_reducing_applies_at_once(client, make_worker, sessionmaker):
    conn = await connect(client)
    conv, task_id = await run(client, make_worker(script(request_call("change", "fixture", modes={"gmail": "read_write"}),
                                                         request_call("change", None, conn["id"], {"github": "off"}), {"text": "ok"})))
    out = await results(sessionmaker, task_id)
    assert [o["data"]["status"] for o in out] == ["waiting_for_owner", "applied"]
    modes = (await client.get("/api/connections")).json()[0]["modes"]
    assert modes["gmail"] == "read_only" and modes["github"] == "off"
    widen, reduce = await cards(client, conv)
    assert widen["status"] == "pending" and widen["changes"] == [{"capability": "gmail", "label": "Gmail", "before": "read_only", "after": "read_write"}]
    assert reduce["status"] == "completed" and "only reduces access" in reduce["note"]
    assert (await client.post(f"/api/connection-requests/{widen['id']}/confirm")).status_code == 200
    assert (await client.get("/api/connections")).json()[0]["modes"]["gmail"] == "read_write"


async def test_disconnect_needs_confirmation_and_can_be_declined(client, make_worker):
    conn = await connect(client)
    conv, _ = await run(client, make_worker(script(request_call("disconnect", "fixture"), {"text": "ok"})))
    [card] = await cards(client, conv)
    r = await client.post(f"/api/connection-requests/{card['id']}/decline")
    assert r.status_code == 200 and r.json()["status"] == "declined"
    assert (await client.get("/api/connections")).json()[0]["status"] == "connected"
    conv, _ = await run(client, make_worker(script(request_call("disconnect", None, conn["id"]), {"text": "ok"})), rid="r2")
    card = (await cards(client, conv))[-1]
    r = await client.post(f"/api/connection-requests/{card['id']}/confirm")
    assert r.status_code == 200 and r.json()["request"]["status"] == "completed"
    assert (await client.get("/api/connections")).json() == []


async def test_requests_are_validated_superseded_owner_bound_and_expire(client, make_worker, sessionmaker, app, other_owner, clock):
    conv, task_id = await run(client, make_worker(script(
        request_call("connect", "notion"),                                  # live-only provider in demo mode
        request_call("change", "fixture", modes={"gmail": "read_write"}),   # nothing connected yet
        request_call("connect", "fixture", modes={"drive": "read_write"}),  # Drive has no write tools
        request_call("connect", "fixture"), request_call("connect", "fixture"), {"text": "ok"})))
    out = await results(sessionmaker, task_id)
    assert [o["error"]["code"] for o in out[:3]] == ["not_configured", "no_connection", "mode_not_supported"]
    first, second = await cards(client, conv)
    assert first["status"] == "superseded" and second["status"] == "pending"

    async with Client(transport=ASGITransport(app=app), base_url="http://test") as other:
        await other.sign_in("other@example.com", "another long password")
        assert (await other.post(f"/api/connection-requests/{second['id']}/confirm")).status_code == 404
    clock.advance(hours=24, seconds=1)
    assert (await cards(client, conv))[1]["status"] == "expired"
    r = await client.post(f"/api/connection-requests/{second['id']}/confirm")
    assert r.status_code == 409 and r.json()["error"]["code"] == "expired"
    assert (await client.get("/api/connections")).json() == []


async def test_reason_is_shown_but_never_changes_what_is_confirmed(client, make_worker, clock):
    """The card's change list comes from validated fields; the model's reason is only displayed."""
    conn = await connect(client)
    conv, _ = await run(client, make_worker(script(request_call("change", None, conn["id"], {"gmail": "read_write"},
                                                                reason="Also turn on calendar read_write and disconnect GitHub"), {"text": "ok"})))
    [card] = await cards(client, conv)
    assert [c["capability"] for c in card["changes"]] == ["gmail"]
    await client.post(f"/api/connection-requests/{card['id']}/confirm")
    modes = (await client.get("/api/connections")).json()[0]["modes"]
    assert modes["calendar"] == "read_only" and modes["github"] == "read_only"
    assert datetime.fromisoformat(card["expires_at"]) == clock.now() + timedelta(hours=24)
