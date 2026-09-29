"""DATA-01 delete-all: cancel work, disconnect and revoke accounts, purge broker records, remove files and rows.

The owner account and its sign-in stay; everything else the owner created is removed. Backups taken
earlier still contain the data until those backups are deleted (disclosed in the response and README).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from opa.api.actions_service import invalidate_actions, open_actions_for_task
from opa.api.tasks_service import transition
from opa.db.models import (TERMINAL_STATES, Action, Approval, Artifact, AuditLog, Automation, BrowserSession, Connection, ConnectionRequest, Conversation, Event, Share,
                           Export, Memory, Notification, OAuthState, Occurrence, Owner, ProviderEvent, Task)

BACKUP_NOTE = ("Backups made before now still contain this data until those backups are deleted. "
               "Provider data (sent email, calendar events) stays with the provider.")


async def erase_owner(sm, settings, clock, owner_id: str, broker=None, browser=None) -> dict[str, Any]:
    # 1. stop everything that could still act, in one transaction
    async with sm() as db, db.begin():
        for task in list(await db.scalars(select(Task).where(Task.owner_id == owner_id, Task.state.notin_(TERMINAL_STATES)))):
            await invalidate_actions(db, await open_actions_for_task(db, task.id), "all data is being deleted", clock)
            await transition(db, task, "cancelled", clock)
        for a in await db.scalars(select(Automation).where(Automation.owner_id == owner_id)):
            a.enabled = False
        conns = list(await db.scalars(select(Connection).where(Connection.owner_id == owner_id)))
        for c in conns:
            c.status = "disconnected"
        sessions = list(await db.scalars(select(BrowserSession).where(BrowserSession.owner_id == owner_id, BrowserSession.state == "active")))
        for s in sessions:
            s.state = "stopped"
    # 2. outside the database: close browsers, revoke provider access, purge broker records
    revocations = []
    for s in sessions:
        if browser is not None:
            try:
                await browser.stop(s.id)
            except Exception:  # noqa: BLE001
                pass
    for c in conns:
        if broker is None:
            revocations.append({"account": c.account_label, "revoked": False, "error": "the connector service is not configured"})
            continue
        try:
            res = await broker.revoke(c.id, owner_id) if c.provider != "fixture" else {"revoked": True, "error": None}
            await broker.purge(c.id, owner_id)
        except Exception as e:  # noqa: BLE001 - report; local deletion continues
            res = {"revoked": False, "error": f"{e.__class__.__name__}"}
        revocations.append({"account": c.account_label, **res})
    # 3. delete rows (dependents first) and reset preferences
    counts: dict[str, int] = {}
    async with sm() as db, db.begin():
        async def wipe(model, *where) -> None:
            n = await db.scalar(select(func.count()).select_from(model).where(*where))
            await db.execute(delete(model).where(*where))
            counts[model.__tablename__] = n or 0
        mine = lambda m: m.owner_id == owner_id  # noqa: E731
        await wipe(Occurrence, mine(Occurrence))
        await wipe(ProviderEvent, mine(ProviderEvent))
        await wipe(Automation, mine(Automation))
        await wipe(Approval, mine(Approval))
        await wipe(Action, mine(Action))
        await wipe(Task, mine(Task))  # jobs cascade
        await wipe(ConnectionRequest, mine(ConnectionRequest))
        await wipe(Share, mine(Share))  # every shared link stops working
        await wipe(Conversation, mine(Conversation))  # messages cascade
        await wipe(Memory, mine(Memory))
        await wipe(Artifact, mine(Artifact))  # versions cascade
        await wipe(Connection, mine(Connection))
        await wipe(OAuthState, mine(OAuthState))
        await wipe(BrowserSession, mine(BrowserSession))
        await wipe(Export, mine(Export))
        await wipe(Notification, mine(Notification))
        await wipe(Event, mine(Event))
        await wipe(AuditLog, AuditLog.owner_id == owner_id)
        owner = await db.get(Owner, owner_id)
        owner.preferences, owner.assistant_name, owner.response_style = {}, "Assistant", "concise"
        db.add(AuditLog(owner_id=owner_id, actor="owner", type="owner.data_deleted", subject_id=owner_id, sanitized={"counts": counts}))
    # 4. files
    for root in (Path(settings.artifact_dir) / owner_id, Path(settings.export_dir) / owner_id):
        shutil.rmtree(root, ignore_errors=True)
    return {"deleted": counts, "revocations": revocations, "backup_note": BACKUP_NOTE}
