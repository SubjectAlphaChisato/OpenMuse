from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import AsyncIterator

import pgserver
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opa.api.app import create_app
from opa.api.auth import hash_password
from opa.broker.app import create_broker_app
from opa.broker.client import BrokerClient
from opa.broker.service import Broker
from opa.core.clock import FakeClock
from opa.core.settings import Settings
from opa.db.migrate import upgrade_head
from opa.db.models import Base, Owner
from opa.worker.loop import Worker
from opa.worker.model import ScriptedAdapter


@pytest.fixture(scope="session")
def pg_url() -> str:
    d = os.path.join(tempfile.gettempdir(), "opa-pytest-pg")
    server = pgserver.get_server(d)
    uri = server.get_uri()  # postgresql://postgres:@/postgres?host=/sock/dir
    url = uri.replace("postgresql://", "postgresql+asyncpg://")
    upgrade_head(url)
    yield url
    server.cleanup()


@pytest.fixture
def settings(pg_url: str) -> Settings:
    return Settings(database_url=pg_url, mode="demo", model_provider="scripted", lease_seconds=60, heartbeat_seconds=10,
                    artifact_dir=tempfile.mkdtemp(), internal_token="test-internal-token", tool_timeout_seconds=5)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc))


@pytest_asyncio.fixture
async def sessionmaker(settings: Settings):
    engine = create_async_engine(settings.database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        names = ", ".join(t.fullname for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    yield sm
    await engine.dispose()


@pytest_asyncio.fixture
async def owner(sessionmaker) -> Owner:
    async with sessionmaker() as db, db.begin():
        o = Owner(email="owner@example.com", password_hash=hash_password("correct horse battery"))
        db.add(o)
    return o


@pytest_asyncio.fixture
async def other_owner(sessionmaker) -> Owner:
    async with sessionmaker() as db, db.begin():
        o = Owner(email="other@example.com", password_hash=hash_password("another long password"))
        db.add(o)
    return o


@pytest.fixture
def broker(settings, sessionmaker, clock) -> Broker:
    return Broker(settings, sessionmaker, clock, read_retry_base=0)


@pytest_asyncio.fixture
async def broker_client(broker) -> AsyncIterator[BrokerClient]:
    """The real broker behind its real HTTP interface, served in-process."""
    bc = BrokerClient("http://broker", broker.settings.internal_token, transport=ASGITransport(app=create_broker_app(broker)))
    yield bc
    await bc.aclose()


@pytest.fixture
def app(settings, clock, sessionmaker, broker_client):
    return create_app(settings=settings, clock=clock, sessionmaker=sessionmaker, broker=broker_client)


class Client(AsyncClient):
    async def sign_in(self, email: str = "owner@example.com", password: str = "correct horse battery") -> None:
        r = await self.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 204, r.text
        me = await self.get("/api/me")
        assert me.status_code == 200, me.text
        self.headers["x-csrf-token"] = me.json()["csrf_token"]

    async def submit(self, conversation_id: str, content: str, rid: str, task_id: str | None = None) -> dict:
        r = await self.post(f"/api/conversations/{conversation_id}/messages", json={"content": content, "client_request_id": rid, "task_id": task_id})
        assert r.status_code == 202, r.text
        return r.json()

    async def default_conversation(self) -> str:
        r = await self.get("/api/conversations")
        assert r.status_code == 200, r.text
        return next(c["id"] for c in r.json() if c["is_default"])


@pytest_asyncio.fixture
async def anon(app) -> AsyncIterator[Client]:
    async with Client(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def client(app, owner) -> AsyncIterator[Client]:
    async with Client(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.sign_in()
        yield c


@pytest.fixture
def make_worker(settings, sessionmaker, clock, broker_client):
    def _make(script: dict | None = None, name: str | None = None) -> Worker:
        return Worker(settings, sessionmaker, ScriptedAdapter(script), clock=clock, name=name, broker=broker_client)

    return _make
