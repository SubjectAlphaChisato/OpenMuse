"""A01 / CHAT-01 / TASK-01: tasks survive refresh, worker restart, and lease loss."""
import pytest

from opa.worker.loop import LeaseLost


async def test_two_tasks_survive_refresh_and_complete_in_order(client, make_worker):
    conv = await client.default_conversation()
    a = await client.submit(conv, "first task", "r1")
    b = await client.submit(conv, "second task", "r2")
    assert a["task_id"] != b["task_id"]

    # "refresh": a fresh listing shows both tasks and both user messages
    tasks = (await client.get("/api/tasks")).json()["items"]
    assert {t["id"] for t in tasks} == {a["task_id"], b["task_id"]}
    assert all(t["state"] == "queued" for t in tasks)
    msgs = (await client.get(f"/api/conversations/{conv}/messages")).json()
    assert [m["content"] for m in msgs] == ["first task", "second task"]
    assert [m["task_id"] for m in msgs] == [a["task_id"], b["task_id"]]

    w = make_worker()
    assert await w.run_once() is True
    assert (await client.get(f"/api/tasks/{a['task_id']}")).json()["state"] == "succeeded"
    assert (await client.get(f"/api/tasks/{b['task_id']}")).json()["state"] == "queued"
    assert await w.run_once() is True
    assert (await client.get(f"/api/tasks/{b['task_id']}")).json()["state"] == "succeeded"
    assert await w.run_once() is False

    msgs = (await client.get(f"/api/conversations/{conv}/messages")).json()
    assert [m["role"] for m in msgs] == ["user", "user", "assistant", "assistant"]
    assert msgs[2]["task_id"] == a["task_id"]


async def test_duplicate_client_request_id(client):
    conv = await client.default_conversation()
    a = await client.submit(conv, "same", "dup")
    b = await client.submit(conv, "same", "dup")
    assert a["task_id"] == b["task_id"] and b["created"] is False
    r = await client.post(f"/api/conversations/{conv}/messages", json={"content": "different", "client_request_id": "dup"})
    assert r.status_code == 409


async def test_worker_crash_recovery_and_fencing(client, make_worker, clock):
    conv = await client.default_conversation()
    await client.submit(conv, "long job", "r1")

    w1 = make_worker(name="w1")
    claim = await w1.claim()  # w1 claims and then "crashes" before finishing
    assert claim is not None
    task_id, job_id, token = claim
    assert (await client.get(f"/api/tasks/{task_id}")).json()["state"] == "running"

    w2 = make_worker(name="w2")
    assert await w2.claim() is None  # lease is live; a second worker cannot claim

    clock.advance(seconds=61)  # lease expires
    assert await w2.recovery_sweep() == 1
    assert (await client.get(f"/api/tasks/{task_id}")).json()["state"] == "queued"

    # The stale worker cannot checkpoint or renew after losing its lease.
    assert await w1._renew(job_id, token) is False
    async with w1.sm() as db:
        with pytest.raises(LeaseLost):
            await w1._fenced(db, task_id, token)

    assert await w2.run_once() is True
    t = (await client.get(f"/api/tasks/{task_id}")).json()
    assert t["state"] == "succeeded"
    assert t["outputs"]["final_message"]["confirmed"] is True


async def test_checkpoint_retained_across_restart(client, make_worker):
    """The worker checkpoints after each tool round; a new worker resumes from the checkpoint."""
    script = {"match": [{"contains": "notes", "steps": [
        {"tool_calls": [{"name": "note", "input": {"key": "k1", "text": "first"}}]},
        {"tool_calls": [{"name": "ask_user", "input": {"question": "Which one?"}}]},
        {"text": "All done."},
    ]}]}
    conv = await client.default_conversation()
    sub = await client.submit(conv, "take notes", "r1")
    assert await make_worker(script, name="w1").run_once()
    t = (await client.get(f"/api/tasks/{sub['task_id']}")).json()
    assert t["state"] == "waiting_user"
    assert t["outputs"]["note:k1"] == {"value": "first", "confirmed": True}

    # reply, then a *different* worker process finishes the task from the stored checkpoint
    reply = await client.submit(conv, "the second one", "r2", task_id=sub["task_id"])
    assert reply["task_id"] == sub["task_id"]
    w2 = make_worker(script, name="w2")
    assert await w2.run_once()
    t = (await client.get(f"/api/tasks/{sub['task_id']}")).json()
    assert t["state"] == "succeeded", t
    assert t["outputs"]["note:k1"]["value"] == "first"  # earlier output not lost
    msgs = (await client.get(f"/api/conversations/{conv}/messages")).json()
    assert [m["content"] for m in msgs if m["role"] == "assistant"] == ["Which one?", "All done."]
