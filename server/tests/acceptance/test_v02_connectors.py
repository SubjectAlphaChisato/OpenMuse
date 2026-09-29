"""v0.2 read-only connectors: Google Drive and Docs, Notion, GitHub (docs/providers/*.md).

Live mode against in-process fakes of each provider's HTTP API; demo mode against the fixture's sample data.
"""
import json
import logging
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select, text

from opa.broker.service import Broker
from opa.broker.vault import generate_kek
from opa.core.settings import Settings
from opa.db.models import BrokerCredential
from opa.worker.browser_tools import private_texts
from tests.fake_google import DRIVE_READ, FakeGoogle
from tests.fake_providers import FakeGitHub, FakeNotion, combined_transport
from tests.helpers import call, script, tool_results

KEK = generate_kek()
CREDS = dict(google_client_id="test-client", google_client_secret="test-secret", notion_client_id="notion-client",
             notion_client_secret="notion-secret", github_client_id="Iv1.test", github_client_secret="gh-secret")


@pytest.fixture
def settings(pg_url):
    return Settings(database_url=pg_url, mode="live", model_provider="scripted", internal_token="test-internal-token",
                    tool_timeout_seconds=5, broker_kek=KEK, **CREDS)


@pytest.fixture
def fakes(clock):
    return {"google": FakeGoogle(clock), "notion": FakeNotion(clock), "github": FakeGitHub(clock)}


@pytest.fixture
def broker(settings, sessionmaker, clock, fakes):
    return Broker(settings, sessionmaker, clock, read_retry_base=0,
                  google_transport=combined_transport(fakes["google"], fakes["notion"], fakes["github"]))


async def sign_in(client, fakes, provider, modes=None, connection_id=None, **consent):
    body = {"connection_id": connection_id} | ({"modes": modes} if modes is not None else {})
    r = await client.post(f"/api/connectors/{provider}/authorize", json=body)
    assert r.status_code == 200, r.text
    url = r.json()["authorization_url"]
    r = await client.get(fakes[provider].consent(url, **consent))
    loc = parse_qs(urlparse(r.headers["location"]).query)
    assert loc["oauth"] == ["connected"], loc
    conn = next(c for c in (await client.get("/api/connections")).json() if c["id"] == loc["connection"][0])
    return conn, url


async def run(client, make_worker, steps, rid="r1"):
    conv = await client.default_conversation()
    sub = await client.submit(conv, "look it up", rid)
    assert await make_worker(script(*steps)).run_once()
    return sub["task_id"]


async def results(sessionmaker, task_id):
    return [json.loads(r["content"]) for r in await tool_results(sessionmaker, task_id)]


# ------------------------------------------------------------------ registry and permissions

async def test_connectors_list_every_provider_with_read_only_new_capabilities(client):
    items = {c["provider"]: c for c in (await client.get("/api/connectors")).json()}
    assert set(items) == {"google", "notion", "github"} and all(c["available"] for c in items.values())
    modes = {d["id"]: d["modes"] for c in items.values() for d in c["capability_details"]}
    assert modes["gmail"] == ["off", "read_only", "read_write"]
    assert modes["drive"] == modes["notion"] == modes["github"] == ["off", "read_only"]


async def test_new_capabilities_refuse_write_mode(client, fakes):
    conn, _ = await sign_in(client, fakes, "notion")
    r = await client.patch(f"/api/connections/{conn['id']}", json={"modes": {"notion": "read_write"}, "expected_version": conn["version"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mode_not_supported"


# ------------------------------------------------------------------ Notion

async def test_notion_sign_in_follows_its_contract(client, fakes, sessionmaker):
    conn, url = await sign_in(client, fakes, "notion")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://api.notion.com/v1/oauth/authorize?") and q["owner"] == "user" and "code_challenge" not in q
    assert q["redirect_uri"] == "http://127.0.0.1:8080/api/oauth/notion/callback"
    assert (conn["provider"], conn["account_label"], conn["modes"]) == ("notion", "Acme (Notion)", {"notion": "read_only"})
    token_req = next(r for r in fakes["notion"].requests if r.url.path == "/v1/oauth/token")
    assert token_req.headers["authorization"].startswith("Basic ") and token_req.headers["content-type"] == "application/json"
    again, _ = await sign_in(client, fakes, "notion")
    assert again["id"] == conn["id"]  # the same workspace reuses its connection


async def test_notion_reads_search_nested_page_and_database(client, fakes, make_worker, sessionmaker):
    conn, _ = await sign_in(client, fakes, "notion")
    cid = conn["id"]
    task = await run(client, make_worker, [
        {"tool_calls": [call("notion.search", cid, query="design", kind=None, page_size=None, start_cursor=None)]},
        {"tool_calls": [call("notion.read_page", cid, page_id="p-notes")]},
        {"tool_calls": [call("notion.query_data_source", cid, data_source_id="ds-tasks", page_size=None, start_cursor=None)]},
        {"tool_calls": [call("notion.read_page", cid, page_id="p-private")]},
        {"text": "done"}])
    search, page, rows, private = await results(sessionmaker, task)
    assert [r["title"] for r in search["data"]["results"]] == ["Design review notes"]
    text_ = page["data"]["content"]["text"]
    assert page["data"]["content"]["untrusted"] is True
    assert "Decisions" in text_ and "- Ship onboarding v2 in October" in text_ and "  Owner: Priya" in text_  # nested child block
    assert "Next review: Thursday" in text_  # fourth block: the adapter followed pagination (two per page)
    assert rows["data"]["rows"][0]["properties"]["Status"] == {"untrusted": True, "text": "In progress", "truncated": False}
    assert private["error"]["code"] == "not_found"


async def test_notion_refreshes_on_401_and_asks_to_reconnect_when_revoked(client, fakes, make_worker, sessionmaker):
    conn, _ = await sign_in(client, fakes, "notion")
    fakes["notion"].tokens.expire_all()
    task = await run(client, make_worker, [{"tool_calls": [call("notion.search", conn["id"], query="", kind=None, page_size=None, start_cursor=None)]}, {"text": "ok"}])
    assert (await results(sessionmaker, task))[0]["status"] == "succeeded" and fakes["notion"].tokens.refresh_calls == 1
    fakes["notion"].tokens.revoke_subject("ws-1")
    task = await run(client, make_worker, [{"tool_calls": [call("notion.search", conn["id"], query="", kind=None, page_size=None, start_cursor=None)]}, {"text": "ok"}], rid="r2")
    assert (await results(sessionmaker, task))[0]["error"]["code"] == "auth_required"
    assert (await client.get("/api/connections")).json()[0]["status"] == "reauth_required"
    assert "Reconnect your Notion account" in [n["title"] for n in (await client.get("/api/notifications")).json()]


async def test_notion_disconnect_revokes_with_client_credentials(client, fakes, sessionmaker):
    conn, _ = await sign_in(client, fakes, "notion")
    r = await client.delete(f"/api/connections/{conn['id']}")
    assert r.json()["revoked"] is True and len(fakes["notion"].revoked) == 1
    async with sessionmaker() as db:
        assert await db.scalar(select(BrokerCredential).where(BrokerCredential.connection_id == conn["id"])) is None


# ------------------------------------------------------------------ GitHub

async def test_github_sign_in_uses_pkce_and_reads_repositories(client, fakes, make_worker, sessionmaker):
    conn, url = await sign_in(client, fakes, "github")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://github.com/login/oauth/authorize?") and q["code_challenge_method"] == "S256" and "scope" not in q
    assert (conn["account_label"], conn["modes"]) == ("octo (GitHub)", {"github": "read_only"})
    cid = conn["id"]
    task = await run(client, make_worker, [
        {"tool_calls": [call("github.list_repos", cid, page=None)]},
        {"tool_calls": [call("github.search_issues", cid, query="repo:octo/app crash", max_results=None)]},
        {"tool_calls": [call("github.get_issue", cid, owner="octo", repo="app", number=5)]},
        {"tool_calls": [call("github.read_file", cid, owner="octo", repo="app", path="README.md", ref=None)]},
        {"tool_calls": [call("github.read_file", cid, owner="octo", repo="app", path="logo.png", ref=None)]},
        {"tool_calls": [call("github.read_file", cid, owner="octo", repo="app", path="src", ref=None)]},
        {"text": "done"}])
    repos, search, issue, readme, binary, directory = await results(sessionmaker, task)
    assert repos["data"]["repos"][0]["full_name"] == "octo/app"
    assert search["data"]["items"][0]["number"] == 5 and search["data"]["items"][0]["repo"] == "octo/app"
    assert issue["data"]["body"]["untrusted"] is True and issue["data"]["comments"][0]["body"]["text"] == "Looking into it"
    assert readme["data"]["content"] == {"untrusted": True, "text": "# App\n\nRun make.\n", "truncated": False}
    assert "not text" in binary["error"]["message"] and "directory" in directory["error"]["message"]


async def test_github_expired_token_refreshes_and_rate_limit_is_retried(client, fakes, make_worker, sessionmaker, clock):
    conn, _ = await sign_in(client, fakes, "github")
    clock.advance(hours=9)  # past the 8-hour access token lifetime
    await client.sign_in()
    fakes["github"].faults.append("rate")
    task = await run(client, make_worker, [{"tool_calls": [call("github.list_repos", conn["id"], page=None)]}, {"text": "ok"}])
    assert (await results(sessionmaker, task))[0]["status"] == "succeeded"
    assert fakes["github"].tokens.refresh_calls == 1


async def test_github_disconnect_revokes_with_basic_auth(client, fakes):
    conn, _ = await sign_in(client, fakes, "github")
    r = await client.delete(f"/api/connections/{conn['id']}")
    assert r.json()["revoked"] is True and len(fakes["github"].revoked) == 1


# ------------------------------------------------------------------ Google Drive and Docs

async def test_drive_needs_its_own_scope_then_reads_docs_sheets_and_text(client, fakes, make_worker, sessionmaker):
    conn, _ = await sign_in(client, fakes, "google")
    assert conn["modes"] == {"gmail": "read_only", "calendar": "read_only", "drive": "off"}  # Drive is opt-in
    r = await client.patch(f"/api/connections/{conn['id']}", json={"modes": {"drive": "read_only"}, "expected_version": conn["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "scope_required" and DRIVE_READ in r.json()["error"]["message"]
    conn, url = await sign_in(client, fakes, "google", modes={"drive": "read_only"}, connection_id=conn["id"])
    assert DRIVE_READ in parse_qs(urlparse(url).query)["scope"][0]
    assert conn["modes"]["drive"] == "read_only" and conn["modes"]["gmail"] == "read_only"
    cid = conn["id"]
    task = await run(client, make_worker, [
        {"tool_calls": [call("drive.search_files", cid, query="Bob's plan", max_results=None, page_token=None)]},
        {"tool_calls": [call("drive.read_file", cid, file_id="gdoc1")]},
        {"tool_calls": [call("drive.read_file", cid, file_id="gsheet1")]},
        {"tool_calls": [call("drive.read_file", cid, file_id="gtxt")]},
        {"tool_calls": [call("drive.read_file", cid, file_id="gimg")]},
        {"text": "done"}])
    assert fakes["google"].last_drive_q == "(name contains 'Bob\\'s plan' or fullText contains 'Bob\\'s plan') and trashed = false"
    search, doc, sheet, txt, img = await results(sessionmaker, task)
    assert len(search["data"]["files"]) == 4
    assert doc["data"]["content"]["text"].startswith("# Plan") and doc["data"]["content"]["untrusted"] is True
    assert sheet["data"]["content"]["text"] == "item,amount\nTools,1200\n"
    assert txt["data"]["content"]["text"] == "Remember the offsite."
    assert "unsupported_format" in img["error"]["message"]


# ------------------------------------------------------------------ secrets and private context

async def test_no_provider_token_leaks(client, fakes, make_worker, sessionmaker, caplog):
    caplog.set_level(logging.DEBUG)
    n, _ = await sign_in(client, fakes, "notion")
    g, _ = await sign_in(client, fakes, "github")
    await run(client, make_worker, [{"tool_calls": [call("notion.search", n["id"], query="", kind=None, page_size=None, start_cursor=None)]},
                                    {"tool_calls": [call("github.list_repos", g["id"], page=None)]}, {"text": "ok"}])
    async with sessionmaker() as db:
        audit = json.dumps([r[0] for r in (await db.execute(text("SELECT sanitized FROM audit_log"))).all()])
        checkpoints = json.dumps([r[0] for r in (await db.execute(text("SELECT checkpoint FROM tasks"))).all()])
    surfaces = [caplog.text, audit, checkpoints, json.dumps((await client.get("/api/connections")).json()),
                json.dumps((await client.get("/api/events/replay")).json())]
    secrets_ = fakes["notion"].tokens.issued + fakes["github"].tokens.issued
    assert secrets_ and not any(s_ in surface for s_ in secrets_ for surface in surfaces)


def test_new_connector_results_count_as_private_context_for_the_browser():
    messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "notion__read_page", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "secret roadmap codename Nightingale"}]}]
    assert private_texts(messages, "") == ["secret roadmap codename Nightingale"]
