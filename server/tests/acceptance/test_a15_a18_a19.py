"""A15 honest status, A18 budget/cancel, A19 notification replay."""


async def test_a18_budget_blocks_looping_model(client, make_worker):
    script = {"match": [{"contains": "loop", "steps": [{"tool_calls": [{"name": "note", "input": {"key": "x", "text": "again"}}]}]}]}
    conv = await client.default_conversation()
    sub = await client.submit(conv, "loop forever", "r1")
    await make_worker(script).run_once()
    t = (await client.get(f"/api/tasks/{sub['task_id']}")).json()
    assert t["state"] == "blocked"
    assert t["blocker_code"] == "budget_exceeded"
    assert t["usage"]["model_calls"] == 30
    # explicit resume extends the budget and the loop hits it again
    r = await client.post(f"/api/tasks/{sub['task_id']}/resume")
    assert r.status_code == 200 and r.json()["state"] == "queued"


async def test_a18_cancel_prevents_dispatch(client, make_worker):
    conv = await client.default_conversation()
    sub = await client.submit(conv, "cancel me", "r1")
    r = await client.post(f"/api/tasks/{sub['task_id']}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert await make_worker().run_once() is False  # cancelled tasks are never claimed
    assert (await client.post(f"/api/tasks/{sub['task_id']}/cancel")).status_code == 409
    r = await client.post(f"/api/tasks/{sub['task_id']}/retry")
    assert r.status_code == 201
    assert (await client.get(f"/api/tasks/{r.json()['task_id']}")).json()["retry_of"] == sub["task_id"]
    msgs = (await client.get(f"/api/conversations/{conv}/messages")).json()
    assert msgs[-1]["role"] == "system" and msgs[-1]["task_id"] == r.json()["task_id"]


async def test_a15_provider_error_is_visible_not_faked(client, make_worker):
    script = {"match": [{"contains": "fail", "steps": [
        {"tool_calls": [{"name": "note", "input": {"key": "partial", "text": "kept"}}]},
        {"stop_reason": "error", "refusal_detail": "provider_unavailable: 503"},
    ]}]}
    conv = await client.default_conversation()
    sub = await client.submit(conv, "please fail", "r1")
    await make_worker(script).run_once()
    t = (await client.get(f"/api/tasks/{sub['task_id']}")).json()
    assert t["state"] == "blocked" and t["blocker_code"] == "provider_error"
    assert t["outputs"]["note:partial"]["value"] == "kept"  # partial outputs retained
    msgs = (await client.get(f"/api/conversations/{conv}/messages")).json()
    assert all(m["role"] == "user" for m in msgs)  # no fabricated assistant reply


async def test_a15_malformed_tool_input_is_an_error_not_success(client, make_worker):
    script = {"match": [{"contains": "bad", "steps": [
        {"tool_calls": [{"name": "note", "input": {"key": "", "text": "x"}}]},
        {"text": "finished"},
    ]}]}
    conv = await client.default_conversation()
    sub = await client.submit(conv, "bad input", "r1")
    await make_worker(script).run_once()
    t = (await client.get(f"/api/tasks/{sub['task_id']}")).json()
    assert t["state"] == "succeeded"
    assert "note:" not in " ".join(t["outputs"].keys())


async def test_a19_replay_delivers_each_event_once(client, make_worker):
    conv = await client.default_conversation()
    await client.submit(conv, "hello", "r1")
    first = (await client.get("/api/events/replay")).json()
    assert [e["type"] for e in first] == ["task.queued", "message.created"]
    cursor = first[-1]["id"]
    await make_worker().run_once()
    second = (await client.get(f"/api/events/replay?after={cursor}")).json()
    types = [e["type"] for e in second]
    assert types == ["task.running", "message.created", "task.succeeded"]
    assert all(e["id"] > cursor for e in second)
    assert (await client.get(f"/api/events/replay?after={second[-1]['id']}")).json() == []

    notes = (await client.get("/api/notifications?unread_only=true")).json()
    assert len(notes) == 1 and notes[0]["type"] == "task.succeeded"
    r = await client.post(f"/api/notifications/{notes[0]['id']}/read")
    assert r.json()["read_at"] is not None
    assert (await client.get("/api/notifications?unread_only=true")).json() == []


async def test_sse_stream_emits_events_with_ids(client):
    conv = await client.default_conversation()
    await client.submit(conv, "hello", "r1")
    r = await client.get("/api/events?after=0&max_events=2")
    assert r.status_code == 200
    body = r.text
    assert "id: 1\nevent: task.queued" in body and "event: message.created" in body
    # resume from the last id delivers nothing new

