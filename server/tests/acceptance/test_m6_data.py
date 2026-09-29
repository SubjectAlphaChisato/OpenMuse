"""DATA-01: export (A16, first half), delete-all, backup and restore into a clean deployment (A16)."""
import io
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pgserver
import pytest
from httpx import ASGITransport
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opa.api.app import create_app
from opa.data.backup import BackupError, backup, restore
from opa.db.models import Action, FixtureState, Memory, Owner
from opa.scheduler.service import Scheduler
from tests.conftest import Client
from tests.helpers import connect, fixture_state, pending_approval, script, send_call

PG_BIN = str(Path(pgserver.__file__).parent / "pginstall" / "bin")
NOTES = b"# Notes\n\nThe secret project codename is Nightingale.\n"


async def populate(client, make_worker, sessionmaker):
    conn = await connect(client, gmail="read_write")
    up = await client.post("/api/artifacts?filename=notes.md", content=NOTES, headers={"content-type": "text/markdown"})
    assert up.status_code == 201, up.text
    auto = await client.post("/api/automations", json={"name": "Daily note", "kind": "daily", "prompt": "Say hi", "time_zone": "UTC",
                                                       "local_time": "18:00", "connection_ids": [], "confirm": True})
    conv = await client.default_conversation()
    await client.submit(conv, "Email Priya the metrics", "r1")
    assert await make_worker(script({"tool_calls": [send_call(conn["id"])]}, {"text": "Sent."})).run_once()
    await pending_approval(client)
    async with sessionmaker() as db, db.begin():
        owner = (await db.scalars(select(Owner))).first()
        db.add(Memory(owner_id=owner.id, text="Prefers short answers", kind="user_statement", scope="global", confirmed=True, tags=["style"], source_ref={}))
    return conn, up.json(), auto.json()


def secrets_of(client, settings, password_hash):
    return [password_hash, client.headers["x-csrf-token"], client.cookies.get("opa_session"), settings.internal_token]


async def test_export_is_complete_schema_valid_and_secret_free(client, make_worker, sessionmaker, settings, app, other_owner):
    conn, art, _ = await populate(client, make_worker, sessionmaker)
    r = await client.post("/api/export")
    assert r.status_code == 201, r.text
    exp = r.json()
    dl = await client.get(f"/api/exports/{exp['id']}/download")
    assert dl.status_code == 200 and dl.headers["x-content-type-options"] == "nosniff"
    z = zipfile.ZipFile(io.BytesIO(dl.content))
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["export_version"] == "1" and manifest["counts"]["messages"] >= 1 and manifest["counts"]["memory"] == 1
    for name, paths in manifest["entities"].items():  # every data file validates against its own schema
        jsonschema.validate(json.loads(z.read(paths["data"])), json.loads(z.read(paths["schema"])))
    versions = json.loads(z.read("data/artifact_versions.json"))
    assert z.read(versions[0]["path"]) == NOTES
    connections = json.loads(z.read("data/connections.json"))
    assert connections[0]["account_label"] == "demo@example.test" and "credential_ref" not in connections[0]
    assert json.loads(z.read("data/actions.json"))[0]["tool_name"] == "gmail.send_message"
    async with sessionmaker() as db:
        pw_hash = (await db.scalars(select(Owner.password_hash))).first()
    blob = b"".join(z.read(n) for n in z.namelist())
    for secret in secrets_of(client, settings, pw_hash):
        assert secret and secret.encode() not in blob
    assert b"password_hash" not in blob and b"code_verifier" not in blob and b"sync_token" not in blob
    async with Client(transport=ASGITransport(app=app), base_url="http://test") as other:
        await other.sign_in("other@example.com", "another long password")
        assert (await other.get(f"/api/exports/{exp['id']}/download")).status_code == 404
        assert (await other.get("/api/exports")).json() == []


async def test_delete_all_needs_typed_confirmation_and_removes_everything(client, make_worker, sessionmaker, settings, clock):
    conn, art, auto = await populate(client, make_worker, sessionmaker)
    await fixture_state(sessionmaker, clock, conn["id"])  # the broker now holds a demo mailbox for this account
    async with sessionmaker() as db:
        assert await db.get(FixtureState, conn["id"]) is not None
    await client.post("/api/export")
    wrong = await client.post("/api/delete-all", json={"confirm": "yes"})
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "confirmation_required"
    r = await client.post("/api/delete-all", json={"confirm": "OWNER@example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"]["tasks"] >= 1 and body["deleted"]["artifacts"] == 1 and body["revocations"][0]["revoked"] is True
    assert "Backups made before now" in body["backup_note"]
    for path in ("/api/tasks", "/api/approvals", "/api/connections", "/api/automations", "/api/artifacts", "/api/exports", "/api/browser-sessions",
                 "/api/shares"):
        got = await client.get(path)
        assert got.status_code == 200, path
        data = got.json()
        assert (data["items"] if isinstance(data, dict) else data) == [], path
    mem = (await client.get("/api/memory")).json()
    assert mem["items"] == [] and mem["suggestions"] == []
    notes = (await client.get("/api/notifications")).json()
    assert notes == []
    assert not (Path(settings.artifact_dir) / (await client.get("/api/me")).json()["id"]).exists()
    assert not (Path(settings.export_dir) / (await client.get("/api/me")).json()["id"]).exists()
    async with sessionmaker() as db:
        assert await db.get(FixtureState, conn["id"]) is None  # the broker purged the demo mailbox
        assert (await db.execute(text("SELECT type FROM audit_log"))).scalars().all() == ["owner.data_deleted"]
    me = (await client.get("/api/me")).json()
    assert me["approval_mode"] == "ask_writes"  # preferences reset, account and sign-in kept


async def test_a16_backup_restores_into_a_clean_deployment_safely(client, make_worker, sessionmaker, settings, clock, broker_client):
    conn, art, auto = await populate(client, make_worker, sessionmaker)
    shared = await client.post(f"/api/conversations/{await client.default_conversation()}/shares", json={})
    assert shared.status_code == 201, shared.text
    async with sessionmaker() as db, db.begin():  # one write was mid-flight when the backup ran
        a = (await db.scalars(select(Action))).first()
        await db.execute(update(Action).where(Action.id == a.id).values(state="dispatching"))
    work = Path(tempfile.mkdtemp(prefix="opa-backup-", dir="/tmp/opa-main" if Path("/tmp/opa-main").is_dir() else None))
    m = backup(settings.database_url, settings.artifact_dir, str(work / "b1"), include_broker=True, bin_dir=PG_BIN)
    assert set(m["files"]) == {"app.dump", "broker.dump", "artifacts.tar.gz"} and m["includes_broker"]

    fresh = pgserver.get_server(str(work / "pg2"))
    url2 = fresh.get_uri().replace("postgresql://", "postgresql+asyncpg://")
    art2 = str(work / "artifacts2")
    try:
        result = restore(url2, art2, str(work / "b1"), bin_dir=PG_BIN)
        assert result["safety"]["automations_paused"] == 1 and result["safety"]["approvals_invalidated"] == 1
        assert result["safety"]["actions_unknown"] == 1
        assert result["safety"]["shares_revoked"] == 1  # a link turned off after the backup must not come back
        with pytest.raises(BackupError, match="not empty"):
            restore(url2, str(work / "artifacts3"), str(work / "b1"), bin_dir=PG_BIN)

        settings2 = settings.model_copy(update={"database_url": url2, "artifact_dir": art2})
        engine2 = create_async_engine(url2)
        sm2 = async_sessionmaker(engine2, expire_on_commit=False)
        app2 = create_app(settings=settings2, clock=clock, sessionmaker=sm2, broker=broker_client)
        async with Client(transport=ASGITransport(app=app2), base_url="http://restored") as c2:
            await c2.sign_in()  # the owner's sign-in came back with the data
            conv = await c2.default_conversation()
            msgs = (await c2.get(f"/api/conversations/{conv}/messages")).json()
            assert msgs[0]["content"] == "Email Priya the metrics"
            dl = await c2.get(f"/api/artifacts/{art['id']}/versions/1/download")
            assert dl.status_code == 200 and dl.content == NOTES
            restored_auto = (await c2.get(f"/api/automations/{auto['id']}")).json()
            assert restored_auto["paused_reason"] == "restored"
            assert "Restored from a backup" in [n["title"] for n in (await c2.get("/api/notifications")).json()]
            tasks = (await c2.get("/api/tasks")).json()["items"]
            assert any(t["blocker_code"] == "restored" or t["state"] == "blocked" for t in tasks)
            assert (await c2.get("/api/actions?state=unknown")).json()  # the in-flight write must be checked, never re-sent
            assert (await c2.get("/api/approvals")).json() == []  # a pending approval never survives a restore
            s2 = Scheduler(settings2, sm2, clock, broker_client)
            clock.set(datetime(2026, 9, 27, 18, 0, 5, tzinfo=timezone.utc))
            await c2.sign_in()
            await s2.tick()
            assert (await c2.get(f"/api/automations/{auto['id']}")).json()["recent"][0]["state"] == "missed_paused"  # paused, not run
            resumed = await c2.patch(f"/api/automations/{auto['id']}", json={"expected_version": restored_auto["version"], "resume": True})
            assert resumed.json()["paused_reason"] is None and resumed.json()["next_run_local"] == "Mon 2026-09-28 18:00 UTC"
        await engine2.dispose()
    finally:
        fresh.cleanup()

    tampered = work / "b2"
    shutil.copytree(work / "b1", tampered)
    with (tampered / "artifacts.tar.gz").open("ab") as f:
        f.write(b"x")
    with pytest.raises(BackupError, match="does not match"):
        restore(url2, str(work / "a4"), str(tampered), bin_dir=PG_BIN)
    shutil.rmtree(work, ignore_errors=True)
