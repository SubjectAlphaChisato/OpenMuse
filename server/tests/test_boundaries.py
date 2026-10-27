"""Trust boundaries: database roles, broker on its restricted role, credential vault, egress proxy, internal auth."""
import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opa.broker.app import create_broker_app
from opa.broker.service import Broker
from opa.broker.vault import Vault, VaultError, generate_kek
from opa.db.roles import apply_roles
from opa.proxy import AllowlistPolicy, is_public_address, serve
from tests.helpers import connect, decide, pending_approval, script, send_call, sent_messages, task_state


def _role_url(pg_url: str, role: str) -> str:
    return pg_url.replace("postgres:@", f"{role}:pw@")


@pytest.fixture(scope="module")
def roles(pg_url):
    apply_roles(pg_url, "pw", "pw")
    return pg_url


@pytest_asyncio.fixture
async def role_session(roles):
    engines = []

    def make(role):
        eng = create_async_engine(_role_url(roles, role))
        engines.append(eng)
        return async_sessionmaker(eng, expire_on_commit=False)

    yield make
    for e in engines:
        await e.dispose()


async def _denied(sm, sql: str) -> bool:
    async with sm() as db:
        try:
            await db.execute(text(sql))
            return False
        except ProgrammingError as e:
            return "permission denied" in str(e)


async def test_app_role_cannot_read_broker_schema(role_session, sessionmaker):
    app = role_session("opa_app")
    assert await _denied(app, "SELECT * FROM broker.credentials")
    assert await _denied(app, "SELECT * FROM broker.dispatch_claims")
    async with app() as db:
        await db.execute(text("SELECT id, canonical_args FROM actions"))  # full access to application tables


async def test_broker_role_is_least_privilege(role_session):
    b = role_session("opa_broker")
    assert await _denied(b, "SELECT password_hash FROM users")
    assert await _denied(b, "SELECT checkpoint FROM tasks")
    assert await _denied(b, "SELECT intent FROM tasks")
    assert await _denied(b, "SELECT content FROM messages")
    assert await _denied(b, "SELECT * FROM sessions")
    assert await _denied(b, "UPDATE tasks SET state = 'queued'")
    assert await _denied(b, "SELECT payload FROM events")
    assert await _denied(b, "SELECT body FROM notifications")
    async with b() as db:
        await db.execute(text("SELECT id, preferences FROM users"))
        await db.execute(text("SELECT id, owner_id, state FROM tasks"))
        await db.execute(text("SELECT * FROM broker.credentials"))


async def test_full_approved_send_with_broker_on_restricted_role(role_session, settings, clock, client, sessionmaker, make_worker, app):
    """The broker performs tools listing, prepare, dispatch, and audit while connected as opa_broker."""
    from opa.broker.client import BrokerClient
    from opa.worker.loop import Worker
    from opa.worker.model import ScriptedAdapter

    restricted = Broker(settings, role_session("opa_broker"), clock, read_retry_base=0)
    bc = BrokerClient("http://broker", settings.internal_token, transport=ASGITransport(app=create_broker_app(restricted)))
    conn = await connect(client, gmail="read_write")
    conv = await client.default_conversation()
    sub = await client.submit(conv, "send it", "r1")
    w = Worker(settings, sessionmaker, ScriptedAdapter(script({"tool_calls": [send_call(conn["id"])]}, {"text": "Sent."})), clock=clock, broker=bc)
    assert await w.run_once()
    t = await task_state(client, sub["task_id"])
    assert t["state"] == "waiting_approval", t
    await decide(client, await pending_approval(client))
    assert await w.run_once()
    t = await task_state(client, sub["task_id"])
    assert t["state"] == "succeeded", (t["blocker_code"], t["blocker_detail"])
    assert len(await sent_messages(sessionmaker, clock, conn["id"])) == 1
    await bc.aclose()


async def test_vault_round_trip_and_binding(sessionmaker):
    v = Vault(generate_kek())
    async with sessionmaker() as db, db.begin():
        await v.store(db, "conn-a", {"refresh_token": "secret-r"})
    async with sessionmaker() as db:
        assert await v.load(db, "conn-a") == {"refresh_token": "secret-r"}
        raw = (await db.execute(text("SELECT secret_enc FROM broker.credentials"))).scalar()
        assert b"secret-r" not in raw
    with pytest.raises(VaultError):
        async with sessionmaker() as db:
            await Vault(generate_kek()).load(db, "conn-a")  # wrong key-encryption key
    with pytest.raises(VaultError):
        Vault("not-base64!!")


async def test_broker_requires_internal_token(broker):
    async with AsyncClient(transport=ASGITransport(app=create_broker_app(broker)), base_url="http://b") as c:
        r = await c.post("/internal/tools", json={"task_id": "x", "lease_token": "y"})
        assert r.status_code == 401
        r = await c.post("/internal/tools", json={"task_id": "x", "lease_token": "y"}, headers={"authorization": "Bearer wrong"})
        assert r.status_code == 401


# ------------------------------------------------------------------ egress proxy (T-NET-01, policy half)

@pytest.mark.parametrize("ip, public", [
    ("1.1.1.1", True), ("10.0.0.5", False), ("192.168.1.1", False), ("127.0.0.1", False), ("169.254.169.254", False),
    ("100.64.0.1", False), ("::1", False), ("fd00::1", False), ("::ffff:127.0.0.1", False), ("2606:4700::1111", True),
])
def test_address_classification(ip, public):
    assert is_public_address(ip) is public


@pytest_asyncio.fixture
async def echo_server():
    async def handle(r, w):
        while data := await r.read(1024):
            w.write(data)
            await w.drain()
        w.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()


async def _proxy(policy):
    server = await serve("127.0.0.1", 0, policy)
    return server, server.sockets[0].getsockname()[1]


async def _request(proxy_port: int, raw: bytes) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
    w.write(raw)
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    return head, r, w


async def test_proxy_tunnels_only_allowlisted_destination(echo_server):
    server, port = await _proxy(AllowlistPolicy([f"127.0.0.1:{echo_server}"], require_public=False))
    head, r, w = await _request(port, f"CONNECT 127.0.0.1:{echo_server} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert head.startswith(b"HTTP/1.1 200")
    w.write(b"ping")
    await w.drain()
    assert await asyncio.wait_for(r.read(4), 5) == b"ping"
    w.close()

    head, _, w = await _request(port, b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403")
    w.close()
    head, _, w = await _request(port, f"GET http://127.0.0.1:{echo_server}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert head.startswith(b"HTTP/1.1 403")  # plain HTTP forwarding is never allowed
    w.close()
    server.close()


async def test_proxy_refuses_allowlisted_name_resolving_to_private_address(echo_server):
    server, port = await _proxy(AllowlistPolicy([f"localhost:{echo_server}"], require_public=True))
    head, _, w = await _request(port, f"CONNECT localhost:{echo_server} HTTP/1.1\r\n\r\n".encode())
    assert head.startswith(b"HTTP/1.1 403")
    w.close()
    server.close()
