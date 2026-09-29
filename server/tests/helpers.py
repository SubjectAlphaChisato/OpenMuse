"""Shared helpers for M2 tests: connections, scripted tool calls, fixture faults, approvals."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, text

from opa.broker.fixture import FixtureBackend
from opa.db.models import Action, FixtureState, Task


async def connect(client, gmail: str = "read_only", calendar: str = "read_only") -> dict[str, Any]:
    r = await client.post("/api/connections", json={"provider": "fixture"})
    assert r.status_code == 201, r.text
    conn = r.json()
    assert conn["modes"] == {c: "read_only" for c in ("gmail", "calendar", "drive", "notion", "github")}  # APP-01: every capability read only
    if (gmail, calendar) != ("read_only", "read_only"):
        r = await client.patch(f"/api/connections/{conn['id']}", json={"modes": {"gmail": gmail, "calendar": calendar}, "expected_version": conn["version"]})
        assert r.status_code == 200, r.text
        conn = r.json()
    return conn


def call(tool: str, conn_id: str, **inp: Any) -> dict[str, Any]:
    return {"name": tool.replace(".", "__"), "input": {"connection_id": conn_id, **inp}}


def script(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"match": [{"contains": "", "steps": list(steps)}]}


def send_call(conn_id: str, **over: Any) -> dict[str, Any]:
    args = {"to": ["priya@partner.example"], "cc": [], "bcc": [], "subject": "Metrics for Thursday", "body": "Hi Priya, the metrics are attached below.\n42", "in_reply_to_message_id": None}
    args.update(over)
    return call("gmail.send_message", conn_id, **args)


def when(local: str, zone: str = "America/Los_Angeles") -> dict[str, Any]:
    return {"date_time": local, "date": None, "time_zone": zone}


async def fixture_state(sm, clock, conn_id: str) -> dict[str, Any]:
    return await FixtureBackend(sm, conn_id, clock)._snapshot()


async def sent_messages(sm, clock, conn_id: str) -> list[dict[str, Any]]:
    s = await fixture_state(sm, clock, conn_id)
    return [m for m in s["messages"] if "SENT" in m["labels"] and not m["message_id_header"].startswith("<fixture.")]


async def set_fault(sm, clock, conn_id: str, tool: str, kind: str) -> None:
    await fixture_state(sm, clock, conn_id)  # ensure the fixture account exists
    async with sm() as db, db.begin():
        row = await db.get(FixtureState, conn_id)
        row.faults = [*row.faults, {"tool": tool, "kind": kind}]


async def mutate_fixture(sm, clock, conn_id: str, fn) -> None:
    await fixture_state(sm, clock, conn_id)
    async with sm() as db, db.begin():
        state = json.loads(json.dumps((await db.get(FixtureState, conn_id)).state))
        fn(state)
        await db.execute(text("UPDATE broker.fixture_state SET state = CAST(:s AS jsonb) WHERE connection_id = :c"), {"s": json.dumps(state), "c": conn_id})


async def pending_approval(client) -> dict[str, Any]:
    r = await client.get("/api/approvals")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1, items
    return items[0]


async def decide(client, approval: dict[str, Any], decision: str = "allow_once", expected: str | None = None):
    return await client.post(f"/api/approvals/{approval['id']}/decision", json={"decision": decision, "expected_args_hash": expected or approval["args_hash"]})


async def task_state(client, task_id: str) -> dict[str, Any]:
    return (await client.get(f"/api/tasks/{task_id}")).json()


async def tool_results(sm, task_id: str) -> list[dict[str, Any]]:
    async with sm() as db:
        task = await db.get(Task, task_id)
    out = []
    for m in task.checkpoint.get("messages", []):
        if m["role"] == "user" and isinstance(m["content"], list):
            out += [b for b in m["content"] if b.get("type") == "tool_result"]
    return out


async def action_of(sm, task_id: str) -> Action:
    async with sm() as db:
        return (await db.scalars(select(Action).where(Action.task_id == task_id).order_by(Action.created_at.desc()))).first()
