from __future__ import annotations

import asyncio
import getpass
import logging

import typer

from opa.core.settings import get_settings

app = typer.Typer(help="Open Personal Agent")


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    from opa.db.migrate import upgrade_head

    upgrade_head(get_settings().database_url)
    typer.echo("migrations applied")


@app.command("bootstrap-owner")
def bootstrap_owner(email: str, reset_password: bool = False) -> None:
    """Create the single owner. The password is read from a hidden prompt (AUTH-01)."""
    from sqlalchemy import select

    from opa.api.auth import hash_password
    from opa.db.engine import get_sessionmaker
    from opa.db.models import Owner

    async def run() -> None:
        async with get_sessionmaker()() as db, db.begin():
            existing = await db.scalar(select(Owner))
            if existing and not reset_password:
                raise typer.Exit(code=typer.echo("an owner already exists; use --reset-password to change the password") or 1)
            pw = getpass.getpass("Owner password: ")
            if len(pw) < 12:
                typer.echo("password must be at least 12 characters")
                raise typer.Exit(code=1)
            if pw != getpass.getpass("Repeat password: "):
                typer.echo("passwords do not match")
                raise typer.Exit(code=1)
            if existing:
                existing.password_hash = hash_password(pw)
            else:
                db.add(Owner(email=email.strip().lower(), password_hash=hash_password(pw)))
        typer.echo("owner ready")

    asyncio.run(run())


@app.command()
def api() -> None:
    """Run the HTTP API (binds to localhost by default)."""
    import uvicorn

    s = get_settings()
    uvicorn.run("opa.api.app:create_app", factory=True, host=s.bind_host, port=s.bind_port)


@app.command()
def worker() -> None:
    """Run the durable worker."""
    from opa.db.engine import get_sessionmaker
    from opa.worker.loop import Worker
    from opa.worker.model import build_adapter

    from opa.broker.client import BrokerClient

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    broker = BrokerClient(s.broker_url, s.internal_token) if s.internal_token else None
    if broker is None:
        logging.warning("OPA_INTERNAL_TOKEN is not set; connector tools are disabled")
    from opa.browser.client import BrowserClient

    browser_client = BrowserClient(s.browser_url, s.internal_token) if s.browser_url and s.internal_token else None
    w = Worker(s, get_sessionmaker(), build_adapter(s), broker=broker, browser=browser_client)
    asyncio.run(w.run_forever())


@app.command()
def broker() -> None:
    """Run the connector broker (internal network only)."""
    import uvicorn
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from opa.broker.app import create_broker_app
    from opa.broker.service import Broker
    from opa.core.clock import SystemClock

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    engine = create_async_engine(s.broker_database_url or s.database_url, pool_pre_ping=True)
    b = Broker(s, async_sessionmaker(engine, expire_on_commit=False), SystemClock())
    uvicorn.run(create_broker_app(b, sweep_interval=30), host=s.bind_host, port=s.broker_bind_port)


@app.command()
def scheduler() -> None:
    """Run the scheduler: schedules, calendar-change triggers, held runs, and file clean-up (AUTO-01, AUTO-02)."""
    from opa.broker.client import BrokerClient
    from opa.core.clock import SystemClock
    from opa.db.engine import get_sessionmaker
    from opa.scheduler.service import Scheduler

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    broker = BrokerClient(s.broker_url, s.internal_token) if s.internal_token else None
    if broker is None:
        logging.warning("OPA_INTERNAL_TOKEN is not set; calendar-change triggers cannot poll")
    asyncio.run(Scheduler(s, get_sessionmaker(), SystemClock(), broker).run_forever())


@app.command()
def browser() -> None:
    """Run the isolated browser service (Chromium through the public-web proxy)."""
    import uvicorn

    from opa.browser.app import create_browser_app
    from opa.browser.service import BrowserService

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    svc = BrowserService(s.web_proxy_url)
    app_ = create_browser_app(svc, s.internal_token, reap_interval=60)

    @app_.on_event("startup")
    async def _start() -> None:
        await svc.start()

    @app_.on_event("shutdown")
    async def _stop() -> None:
        await svc.close()

    uvicorn.run(app_, host=s.bind_host, port=s.browser_bind_port)


@app.command("backup")
def backup_cmd(out_dir: str, broker: bool = typer.Option(False, "--broker", help="also dump the encrypted credential store (keep OPA_BROKER_KEK separate)")) -> None:
    """Back up the database (schema public) and the artifact files into an empty directory (DATA-01)."""
    from opa.data.backup import BackupError, backup

    s = get_settings()
    try:
        m = backup(s.database_url, s.artifact_dir, out_dir, include_broker=broker, bin_dir=s.pg_bin_dir)
    except BackupError as e:
        typer.echo(f"backup failed: {e}")
        raise typer.Exit(code=1)
    typer.echo(f"backup written to {out_dir} (revision {m['alembic_revision']}, {len(m['files'])} files)")


@app.command("restore")
def restore_cmd(src_dir: str) -> None:
    """Restore a backup into an EMPTY database and artifact directory, then pause automations and cancel pending approvals."""
    from opa.data.backup import BackupError, restore

    s = get_settings()
    try:
        r = restore(s.database_url, s.artifact_dir, src_dir, bin_dir=s.pg_bin_dir)
    except BackupError as e:
        typer.echo(f"restore failed: {e}")
        raise typer.Exit(code=1)
    typer.echo(f"restored; safety steps: {r['safety']}. Now run `opa db-roles` and review paused automations.")


@app.command("db-roles")
def db_roles() -> None:
    """Create or update the opa_app and opa_broker roles and their grants. Run after every migration."""
    import os

    from opa.db.roles import apply_roles

    app_pw, broker_pw = os.environ.get("OPA_APP_DB_PASSWORD"), os.environ.get("OPA_BROKER_DB_PASSWORD")
    if not app_pw or not broker_pw:
        app_pw = app_pw or getpass.getpass("Password for opa_app: ")
        broker_pw = broker_pw or getpass.getpass("Password for opa_broker: ")
    apply_roles(get_settings().database_url, app_pw, broker_pw)
    typer.echo("roles and grants applied")


@app.command()
def proxy(listen: str = "0.0.0.0:3128", policy: str = typer.Option("allowlist", help="allowlist (model host) or public-web (browser)"),
          allow: list[str] = typer.Option([], help="host:port allowed through, repeatable (allowlist policy)"),
          fixture_host: list[str] = typer.Option([], help="demo only: name=ip:port treated as a public site (public-web policy)")) -> None:
    """Run an egress proxy: the worker's route to the model API, or the browser's route to the public web."""
    from opa.proxy import AllowlistPolicy, PublicWebPolicy, serve

    logging.basicConfig(level=logging.INFO)
    host, _, port = listen.rpartition(":")
    if policy == "public-web":
        if fixture_host and get_settings().mode != "demo":
            typer.echo("--fixture-host is only allowed in demo mode")
            raise typer.Exit(code=2)
        resolver, override = {}, {}
        for item in fixture_host:
            name, _, target = item.partition("=")
            ip, _, p = target.rpartition(":")
            resolver[name], override[name] = ip, int(p)
        pol = PublicWebPolicy(resolver=resolver or None, public_override=override or None)
    elif policy == "allowlist":
        if not allow:
            typer.echo("the allowlist policy needs at least one --allow host:port")
            raise typer.Exit(code=2)
        pol = AllowlistPolicy(allow)
    else:
        typer.echo("policy must be allowlist or public-web")
        raise typer.Exit(code=2)

    async def run() -> None:
        server = await serve(host, int(port), pol)
        async with server:
            await server.serve_forever()

    asyncio.run(run())


@app.command("artifacts-gc")
def artifacts_gc(min_age_minutes: int = 60) -> None:
    """Delete artifact files that no version references (failed uploads, deleted files). ART-01."""
    from datetime import timedelta

    from opa.artifacts.service import gc_orphans
    from opa.db.engine import get_sessionmaker

    removed = asyncio.run(gc_orphans(get_sessionmaker(), get_settings(), timedelta(minutes=min_age_minutes)))
    typer.echo(f"removed {removed} unreferenced file(s)")


@app.command("generate-kek")
def generate_kek() -> None:
    """Print a new broker key-encryption key for OPA_BROKER_KEK."""
    from opa.broker.vault import generate_kek as gen

    typer.echo(gen())


if __name__ == "__main__":
    app()
