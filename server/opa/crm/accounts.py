"""CRM accounts: the rules the database cannot express on its own (docs/crm/accounts-schema.md, section 4).

- Visibility: an admin sees every client of the organization; an advisor sees the clients assigned to them;
  a partner user sees no clients. `visible_client_ids` is the one function every client query must go through.
- A client has exactly one primary advisor; assigning a new primary ends the old assignment (history is kept).
- Deactivating a user ends their sessions at once, refuses while they still hold clients, and never removes
  the last active admin.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.core.errors import Conflict, Invalid
from opa.db.models import Client, ClientAssignment, Session, User

ASSIGNABLE_ROLES = ("admin", "advisor")


def _active(user: User) -> bool:
    return user.status == "active"


async def visible_client_ids(db: AsyncSession, user: User) -> list[str] | None:
    """None means every client of the user's organization (admins). Partners and inactive users see nothing."""
    if not _active(user):
        return []
    if user.role == "admin":
        return None
    if user.role != "advisor":
        return []
    rows = await db.scalars(select(ClientAssignment.client_id).where(ClientAssignment.user_id == user.id, ClientAssignment.ended_at.is_(None)))
    return sorted(set(rows))


async def can_see_client(db: AsyncSession, user: User, client: Client) -> bool:
    if client.org_id != user.org_id:
        return False
    ids = await visible_client_ids(db, user)
    return ids is None or client.id in ids


async def assign_client(db: AsyncSession, *, client: Client, user: User, kind: str, by: User, now: datetime,
                        can_edit: bool = False) -> ClientAssignment:
    """Give `user` access to `client`. A new primary replaces the current one; a collaborator is added once."""
    if kind not in ("primary", "collaborator"):
        raise Invalid("kind must be primary or collaborator")
    if user.org_id != client.org_id or by.org_id != client.org_id:
        raise Invalid("the client, the advisor, and the assigner must belong to the same organization")
    if user.role not in ASSIGNABLE_ROLES:
        raise Invalid(f"a {user.role} account cannot be assigned clients", code="role_not_assignable")
    if not _active(user):
        raise Conflict(f"{user.email} is {user.status}; only active accounts can be assigned clients", code="user_inactive")
    current = list(await db.scalars(select(ClientAssignment).where(ClientAssignment.client_id == client.id, ClientAssignment.ended_at.is_(None))
                                    .with_for_update()))
    if kind == "primary":
        for a in current:
            if a.kind == "primary":
                if a.user_id == user.id:
                    return a  # already the primary advisor
                a.ended_at, a.ended_reason = now, f"transferred to {user.email}"
            elif a.user_id == user.id:
                a.ended_at, a.ended_reason = now, "promoted to primary"
    else:
        for a in current:
            if a.user_id == user.id:
                if a.kind == "collaborator" and a.can_edit != can_edit:
                    a.can_edit = can_edit
                return a  # already has access
    row = ClientAssignment(org_id=client.org_id, client_id=client.id, user_id=user.id, kind=kind, can_edit=kind == "primary" or can_edit,
                           assigned_by=by.id, assigned_at=now)
    db.add(row)
    await db.flush()
    return row


async def end_assignment(db: AsyncSession, assignment: ClientAssignment, *, reason: str, now: datetime) -> None:
    """Revoke access. Reductions apply at once; the row stays as history."""
    if assignment.ended_at is None:
        assignment.ended_at, assignment.ended_reason = now, reason[:200]


async def active_assignments(db: AsyncSession, user: User) -> list[ClientAssignment]:
    return list(await db.scalars(select(ClientAssignment).where(ClientAssignment.user_id == user.id, ClientAssignment.ended_at.is_(None))))


async def deactivate_user(db: AsyncSession, user: User, *, by: User, now: datetime) -> None:
    """Deactivate in one transaction: status, every session, and a guard that no client is left without an advisor."""
    if user.status == "deactivated":
        return
    if user.role == "admin":
        others = await db.scalar(select(func.count()).select_from(User).where(
            User.org_id == user.org_id, User.role == "admin", User.status == "active", User.id != user.id))
        if not others:
            raise Conflict("the organization must keep at least one active admin", code="last_admin")
    held = await active_assignments(db, user)
    if held:
        raise Conflict(f"{user.email} still holds {len(held)} client(s); transfer them first", code="clients_not_transferred")
    user.status, user.deactivated_at, user.deactivated_by = "deactivated", now, by.id
    await db.execute(delete(Session).where(Session.owner_id == user.id))  # every device is signed out at once
