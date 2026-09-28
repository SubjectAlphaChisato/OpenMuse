

async def test_anonymous_is_rejected(anon, owner):
    for path in ["/api/me", "/api/conversations", "/api/tasks", "/api/notifications", "/api/events/replay"]:
        r = await anon.get(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "unauthenticated"
        assert "request_id" in r.json()["error"]


async def test_healthz_has_no_private_content(anon):
    r = await anon.get("/healthz")
    assert r.json() == {"ok": True}


async def test_bad_password_then_throttle(anon, owner):
    for _ in range(5):
        r = await anon.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong"})
        assert r.status_code == 401
    r = await anon.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"})
    assert r.status_code == 429


async def test_csrf_required_for_mutations(client):
    conv = await client.default_conversation()
    del client.headers["x-csrf-token"]
    r = await client.post(f"/api/conversations/{conv}/messages", json={"content": "hi", "client_request_id": "r1"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"


async def test_logout_invalidates_session(client):
    r = await client.post("/api/auth/logout")
    assert r.status_code == 204
    r = await client.get("/api/me")
    assert r.status_code == 401


async def test_session_expiry(client, clock):
    clock.advance(days=15)
    r = await client.get("/api/me")
    assert r.status_code == 401


async def test_me_reports_demo_mode(client):
    me = (await client.get("/api/me")).json()
    assert me["mode"] == "demo"
    assert me["setup_required"] == []
