"""A14: another owner cannot read, change, or receive another owner's objects."""
from httpx import ASGITransport

from tests.conftest import Client


async def test_other_owner_sees_nothing(app, client, other_owner, make_worker):
    conv = await client.default_conversation()
    sub = await client.submit(conv, "hello", "r1")
    await make_worker().run_once()
    notes = (await client.get("/api/notifications")).json()
    assert notes, "owner should have a completion notification"

    async with Client(transport=ASGITransport(app=app), base_url="http://test") as other:
        await other.sign_in("other@example.com", "another long password")
        assert (await other.get(f"/api/tasks/{sub['task_id']}")).status_code == 404
        assert (await other.get(f"/api/conversations/{conv}/messages")).status_code == 404
        assert (await other.post(f"/api/tasks/{sub['task_id']}/cancel")).status_code == 404
        assert (await other.post(f"/api/notifications/{notes[0]['id']}/read")).status_code == 404
        assert (await other.get("/api/events/replay")).json() == []
        assert (await other.get("/api/tasks")).json()["items"] == []
