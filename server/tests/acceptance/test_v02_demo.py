"""Demo mode: the fixture account serves all nine v0.2 read tools without any provider."""
import json

from tests.helpers import call, connect, script, tool_results


async def test_demo_account_reads_drive_notion_and_github(client, make_worker, sessionmaker):
    conn = await connect(client)
    cid = conn["id"]
    conv = await client.default_conversation()
    sub = await client.submit(conv, "summarize my work", "r1")
    steps = [
        {"tool_calls": [call("drive.search_files", cid, query="roadmap", max_results=None, page_token=None)]},
        {"tool_calls": [call("drive.read_file", cid, file_id="d-roadmap")]},
        {"tool_calls": [call("notion.search", cid, query="", kind=None, page_size=None, start_cursor=None)]},
        {"tool_calls": [call("notion.read_page", cid, page_id="n-meeting-notes")]},
        {"tool_calls": [call("notion.query_data_source", cid, data_source_id="n-tasks-db", page_size=None, start_cursor=None)]},
        {"tool_calls": [call("github.list_repos", cid, page=None)]},
        {"tool_calls": [call("github.search_issues", cid, query="pricing", max_results=None)]},
        {"tool_calls": [call("github.get_issue", cid, owner="demo-org", repo="website", number=42)]},
        {"tool_calls": [call("github.read_file", cid, owner="demo-org", repo="website", path="README.md", ref=None)]},
        {"text": "Here is your summary."}]
    assert await make_worker(script(*steps)).run_once()
    out = [json.loads(r["content"]) for r in await tool_results(sessionmaker, sub["task_id"])]
    assert all(r["status"] == "succeeded" for r in out), [r.get("error") for r in out]
    assert out[1]["data"]["content"]["text"].startswith("# Q4 roadmap")
    assert out[3]["data"]["content"]["untrusted"] is True and "attacker@evil.example" in out[3]["data"]["content"]["text"]
    assert [i["number"] for i in out[6]["data"]["items"]] == [42]
    assert out[8]["data"]["content"]["text"].startswith("# Website")
