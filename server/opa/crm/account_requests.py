"""CRM account changes as requests (docs/crm/accounts-schema.md, sections 3 and 9).

Who may ask: admins and advisors (the assistant acts on behalf of whoever is signed in). Partner users may not.
What happens:
- Adding access (invite, change role, assign or transfer a client, reactivate) always waits for an admin's
  confirmation through the API. An advisor's request of any kind waits for an admin.
- An admin's own reduction (unassign, suspend, deactivate, revoke sessions) applies at once and is recorded.
- The card renders from `payload`, which holds only validated, resolved fields; `reason` is shown as a quote.
- Requests are single-use, organization-bound, expire after 24 hours, and a newer request about the same
  person or client replaces an older open one. An invitation link is returned once, never stored.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.core.canonical import sha256_hex
from opa.core.errors import Conflict, Forbidden, Invalid, NotFound
from opa.crm import accounts
from opa.db.models import ROLES, AccountRequest, Client, ClientAssignment, Invitation, Partner, Session, User

TTL = timedelta(hours=24)
INVITATION_TTL = timedelta(days=7)
WIDENING = ("invite", "change_role", "assign_client", "transfer_client", "reactivate")
REDUCTIONS = ("unassign_client", "suspend", "deactivate", "revoke_sessions")
ROLE_LABEL = {"admin": "管理员", "advisor": "顾问", "partner": "合伙人"}


class AccountRequestOut(BaseModel):
    id: str
    action: str
    title: str
    lines: list[str]
    note: str
    reason: str
    requested_by: str
    requires_admin: bool
    status: str
    result: str | None
    confirm_label: str
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None


# ------------------------------------------------------------------ lookups and validation


async def _user(db: AsyncSession, org_id: str, user_id: str | None, email: str | None) -> User:
    if user_id:
        u = await db.get(User, user_id)
    elif email:
        u = await db.scalar(select(User).where(User.org_id == org_id, User.email == email.strip().lower()))
    else:
        raise Invalid("give a user id or an email")
    if u is None or u.org_id != org_id:
        raise Invalid("no such member of this organization", code="unknown_user")
    return u


async def _client(db: AsyncSession, org_id: str, client_id: str | None, name: str | None) -> Client:
    if client_id:
        c = await db.get(Client, client_id)
    elif name:
        rows = list(await db.scalars(select(Client).where(Client.org_id == org_id, Client.name == name.strip(), Client.status == "active")))
        if len(rows) > 1:
            raise Invalid(f"more than one client is named {name}; use the client id", code="ambiguous")
        c = rows[0] if rows else None
    else:
        raise Invalid("give a client id or a client name")
    if c is None or c.org_id != org_id:
        raise Invalid("no such client", code="unknown_client")
    return c


async def _partner(db: AsyncSession, org_id: str, partner_id: str | None) -> Partner | None:
    if not partner_id:
        return None
    p = await db.get(Partner, partner_id)
    if p is None or p.org_id != org_id:
        raise Invalid("no such partner firm", code="unknown_partner")
    return p


async def _validate(db: AsyncSession, requester: User, action: str, raw: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Resolve names and ids into a payload the card can render. Returns (payload, target_user_id)."""
    org = requester.org_id
    if action == "invite":
        email = str(raw.get("email", "")).strip().lower()
        role = raw.get("role")
        if "@" not in email:
            raise Invalid("a valid email is required")
        if role not in ROLES:
            raise Invalid("role must be admin, advisor, or partner")
        if await db.scalar(select(User.id).where(User.email == email)):
            raise Conflict(f"{email} already has an account", code="already_member")
        partner = await _partner(db, org, raw.get("partner_id"))
        if (role == "partner") != (partner is not None):
            raise Invalid("a partner invitation needs partner_id, and only a partner invitation may have one")
        assigns = []
        for a in raw.get("initial_assignments") or []:
            if role == "partner":
                raise Invalid("partner users cannot be assigned clients")
            c = await _client(db, org, a.get("client_id"), a.get("client_name"))
            kind = a.get("kind", "collaborator")
            if kind not in ("primary", "collaborator"):
                raise Invalid("assignment kind must be primary or collaborator")
            assigns.append({"client_id": c.id, "client_name": c.name, "kind": kind})
        return {"email": email, "name": str(raw.get("name", "")).strip()[:100], "role": role, "partner_id": partner.id if partner else None,
                "partner_name": partner.name if partner else None, "initial_assignments": assigns}, None
    if action in ("assign_client", "transfer_client", "unassign_client"):
        c = await _client(db, org, raw.get("client_id"), raw.get("client_name"))
        u = await _user(db, org, raw.get("user_id"), raw.get("user_email"))
        payload: dict[str, Any] = {"client_id": c.id, "client_name": c.name, "user_id": u.id, "user_email": u.email, "user_name": u.name}
        if action == "assign_client":
            kind = raw.get("kind", "collaborator")
            if kind not in ("primary", "collaborator"):
                raise Invalid("kind must be primary or collaborator")
            payload |= {"kind": kind, "can_edit": bool(raw.get("can_edit", False))}
            if u.role not in accounts.ASSIGNABLE_ROLES:
                raise Invalid(f"a {u.role} account cannot be assigned clients", code="role_not_assignable")
        if action == "transfer_client":
            current = await db.scalar(select(ClientAssignment).where(ClientAssignment.client_id == c.id, ClientAssignment.kind == "primary",
                                                                     ClientAssignment.ended_at.is_(None)))
            if current and current.user_id == u.id:
                raise Invalid(f"{u.email} is already the primary advisor for {c.name}", code="no_change")
            payload["from_email"] = (await db.get(User, current.user_id)).email if current else None
        if action == "unassign_client":
            a = await db.scalar(select(ClientAssignment).where(ClientAssignment.client_id == c.id, ClientAssignment.user_id == u.id,
                                                               ClientAssignment.ended_at.is_(None)))
            if a is None:
                raise Invalid(f"{u.email} has no access to {c.name}", code="no_change")
            if a.kind == "primary":
                raise Invalid("a client always has a primary advisor; transfer the client instead", code="transfer_instead")
        return payload, u.id
    # everything else targets one member
    u = await _user(db, org, raw.get("user_id") or raw.get("target_user_id"), raw.get("user_email") or raw.get("email"))
    payload = {"user_id": u.id, "user_email": u.email, "user_name": u.name, "user_role": u.role, "user_status": u.status}
    if action == "change_role":
        role = raw.get("role")
        if role not in ROLES:
            raise Invalid("role must be admin, advisor, or partner")
        if role == u.role and raw.get("partner_id", u.partner_id) == u.partner_id:
            raise Invalid(f"{u.email} already has that role", code="no_change")
        partner = await _partner(db, org, raw.get("partner_id"))
        if (role == "partner") != (partner is not None):
            raise Invalid("a partner role needs partner_id, and only a partner role may have one")
        payload |= {"role": role, "partner_id": partner.id if partner else None, "partner_name": partner.name if partner else None}
    elif action in ("suspend", "deactivate"):
        if u.id == requester.id and action == "suspend":
            raise Invalid("you cannot suspend yourself")
        if u.status == "deactivated":
            raise Invalid(f"{u.email} is already deactivated", code="no_change")
    elif action == "reactivate":
        if u.status == "active":
            raise Invalid(f"{u.email} is already active", code="no_change")
    elif action != "revoke_sessions":
        raise Invalid("unknown action")
    return payload, u.id


# ------------------------------------------------------------------ describe


def describe(req: AccountRequest, requester_email: str, now: datetime) -> AccountRequestOut:
    p = req.payload
    who = p.get("user_name") or p.get("user_email", "")
    lines: list[str] = []
    note = ""
    if req.action == "invite":
        title = f"邀请 {p.get('name') or p['email']} 加入"
        lines = [f"邮箱：{p['email']}", f"角色：{ROLE_LABEL[p['role']]}" + (f"（{p['partner_name']}）" if p.get("partner_name") else "")]
        lines += [f"客户：{a['client_name']}（{'主责' if a['kind'] == 'primary' else '协作'}）" for a in p.get("initial_assignments", [])]
        note = "确认后生成邀请链接（只显示一次），对方自己设置密码。"
        confirm = "发出邀请"
    elif req.action == "change_role":
        title = f"把 {who} 改为{ROLE_LABEL[p['role']]}"
        lines = [f"{ROLE_LABEL[p['user_role']]} → {ROLE_LABEL[p['role']]}" + (f"（{p['partner_name']}）" if p.get("partner_name") else "")]
        confirm = "允许"
    elif req.action == "assign_client":
        title = f"让 {who} 看到 {p['client_name']}"
        lines = [f"{'主责顾问' if p['kind'] == 'primary' else '协作者' + ('，可编辑' if p.get('can_edit') else '，只读')}"]
        confirm = "允许"
    elif req.action == "transfer_client":
        title = f"把 {p['client_name']} 转给 {who}"
        lines = [f"主责顾问：{p.get('from_email') or '（无）'} → {p['user_email']}"]
        confirm = "允许"
    elif req.action == "unassign_client":
        title = f"收回 {who} 对 {p['client_name']} 的访问"
        confirm = "收回"
    elif req.action == "suspend":
        title = f"暂停 {who} 的账号"
        note = "立即登出所有设备；随时可以恢复。"
        confirm = "暂停"
    elif req.action == "reactivate":
        title = f"恢复 {who} 的账号"
        confirm = "恢复"
    elif req.action == "deactivate":
        title = f"停用 {who} 的账号（离职/解约）"
        note = "立即登出所有设备。名下客户必须先转出。"
        confirm = "停用"
    else:
        title = f"让 {who} 在所有设备上重新登录"
        confirm = "执行"
    if req.requires_admin and not note:
        note = "需要管理员确认才生效。"
    status = "expired" if req.status == "pending" and req.expires_at <= now else req.status
    return AccountRequestOut(id=req.id, action=req.action, title=title, lines=lines, note=note, reason=req.reason, requested_by=requester_email,
                             requires_admin=req.requires_admin, status=status, result=req.result, confirm_label=confirm, expires_at=req.expires_at,
                             created_at=req.created_at, decided_at=req.decided_at)


async def describe_many(db: AsyncSession, ids: list[str], now: datetime) -> dict[str, AccountRequestOut]:
    if not ids:
        return {}
    reqs = list(await db.scalars(select(AccountRequest).where(AccountRequest.id.in_(ids))))
    emails = {u.id: u.email for u in await db.scalars(select(User).where(User.id.in_({r.requested_by for r in reqs})))}
    return {r.id: describe(r, emails.get(r.requested_by, ""), now) for r in reqs}


# ------------------------------------------------------------------ create, confirm, decline


async def create(db: AsyncSession, clock: Any, *, requester: User, action: str, payload: dict[str, Any], reason: str = "",
                 conversation_id: str | None = None, task_id: str | None = None) -> tuple[AccountRequest, str | None]:
    """Record a request. Returns (request, invite_url); the url is set only when an invitation was created now."""
    if requester.role not in ("admin", "advisor"):
        raise Forbidden("partner accounts cannot change accounts")
    if action not in WIDENING + REDUCTIONS:
        raise Invalid("unknown action")
    now = clock.now()
    clean, target_id = await _validate(db, requester, action, payload)
    requires_admin = action in WIDENING or requester.role != "admin"
    # a newer request about the same person or client replaces an older open one
    for old in await db.scalars(select(AccountRequest).where(
            AccountRequest.org_id == requester.org_id, AccountRequest.action == action, AccountRequest.status == "pending",
            AccountRequest.target_user_id.is_(None) if target_id is None else AccountRequest.target_user_id == target_id).with_for_update()):
        if old.payload.get("client_id") == clean.get("client_id") and old.payload.get("email") == clean.get("email"):
            old.status, old.decided_at = "superseded", now
    req = AccountRequest(org_id=requester.org_id, requested_by=requester.id, conversation_id=conversation_id, task_id=task_id, action=action,
                         target_user_id=target_id, payload=clean, reason=reason.strip()[:300], requires_admin=requires_admin,
                         status="pending", expires_at=now + TTL)
    db.add(req)
    await db.flush()
    url = None
    if not requires_admin:
        url = await _apply(db, req, by=requester, now=now)
    return req, url


async def _load_open(db: AsyncSession, request_id: str, org_id: str, now: datetime) -> AccountRequest:
    req = await db.scalar(select(AccountRequest).where(AccountRequest.id == request_id).with_for_update())
    if req is None or req.org_id != org_id:
        raise NotFound("request not found")
    if req.status != "pending":
        raise Conflict(f"this request is already {req.status}", code="invalid_state")
    if req.expires_at <= now:
        req.status, req.decided_at = "expired", now
        raise Conflict("this request expired; ask again", code="expired")
    return req


async def confirm(db: AsyncSession, request_id: str, admin: User, clock: Any, settings: Any) -> tuple[AccountRequest, str | None]:
    """Only an admin's authenticated call carries out a request. Returns (request, invite_url)."""
    if admin.role != "admin":
        raise Forbidden("only an admin can confirm account changes")
    now = clock.now()
    req = await _load_open(db, request_id, admin.org_id, now)
    url = await _apply(db, req, by=admin, now=now, base_url=settings.public_base_url)
    return req, url


async def decline(db: AsyncSession, request_id: str, user: User, clock: Any) -> AccountRequest:
    now = clock.now()
    req = await _load_open(db, request_id, user.org_id, now)
    if user.role != "admin" and req.requested_by != user.id:
        raise Forbidden("only an admin or the requester can decline")
    req.status, req.decided_by, req.decided_at, req.result = "declined", user.id, now, "已拒绝。"
    return req


async def _apply(db: AsyncSession, req: AccountRequest, *, by: User, now: datetime, base_url: str = "") -> str | None:
    """Carry out a request inside the caller's transaction. A refused change raises; the transaction rolls back, so the
    request row is not kept, and the caller sees the error."""
    p = req.payload
    url = None
    if req.action == "invite":
        token = secrets.token_urlsafe(32)
        db.add(Invitation(org_id=req.org_id, email=p["email"], name=p["name"], role=p["role"], partner_id=p.get("partner_id"),
                          initial_assignments=p.get("initial_assignments", []), token_hash=sha256_hex(token), invited_by=by.id,
                          expires_at=now + INVITATION_TTL))
        url = f"{base_url.rstrip('/')}/invite/{token}"
        result = f"已为 {p['email']} 生成邀请链接，7 天内有效。"
    else:
        target = await db.get(User, req.target_user_id)
        if req.action == "change_role":
            await _change_role(db, target, p["role"], p.get("partner_id"), now)
            result = f"{target.email} 现在是{ROLE_LABEL[p['role']]}。"
        elif req.action in ("assign_client", "transfer_client"):
            client = await db.get(Client, p["client_id"])
            kind = "primary" if req.action == "transfer_client" else p["kind"]
            await accounts.assign_client(db, client=client, user=target, kind=kind, by=by, now=now, can_edit=p.get("can_edit", False))
            result = f"{target.email} 现在可以看到 {client.name}。"
        elif req.action == "unassign_client":
            a = await db.scalar(select(ClientAssignment).where(ClientAssignment.client_id == p["client_id"], ClientAssignment.user_id == target.id,
                                                               ClientAssignment.ended_at.is_(None)))
            if a:
                await accounts.end_assignment(db, a, reason=f"removed by {by.email}", now=now)
            result = f"已收回 {target.email} 对 {p['client_name']} 的访问。"
        elif req.action == "suspend":
            target.status = "suspended"
            await db.execute(delete(Session).where(Session.owner_id == target.id))
            result = f"{target.email} 已暂停并登出。"
        elif req.action == "reactivate":
            target.status, target.deactivated_at, target.deactivated_by = "active", None, None
            result = f"{target.email} 已恢复。"
        elif req.action == "deactivate":
            await accounts.deactivate_user(db, target, by=by, now=now)
            result = f"{target.email} 已停用并登出。"
        else:
            await db.execute(delete(Session).where(Session.owner_id == target.id))
            result = f"{target.email} 已在所有设备上登出。"
    req.status, req.decided_by, req.decided_at, req.result = "completed", by.id, now, result
    return url


async def _change_role(db: AsyncSession, user: User, role: str, partner_id: str | None, now: datetime) -> None:
    if user.role == "admin" and role != "admin":
        others = await db.scalar(select(func.count()).select_from(User).where(
            User.org_id == user.org_id, User.role == "admin", User.status == "active", User.id != user.id))
        if not others:
            raise Conflict("the organization must keep at least one active admin", code="last_admin")
    if role == "partner" and await accounts.active_assignments(db, user):
        raise Conflict(f"{user.email} still holds clients; transfer them before making them a partner user", code="clients_not_transferred")
    user.role, user.partner_id = role, partner_id


# ------------------------------------------------------------------ invitations


async def preview_invitation(db: AsyncSession, token: str, now: datetime) -> Invitation:
    inv = await db.scalar(select(Invitation).where(Invitation.token_hash == sha256_hex(token)))
    if inv is None or inv.accepted_at or inv.revoked_at or inv.expires_at <= now:
        raise NotFound("this invitation is not available")
    return inv


async def accept_invitation(db: AsyncSession, clock: Any, *, token: str, password_hash: str, name: str | None) -> User:
    """Turn an invitation into an active account, apply its client assignments, and spend the link."""
    now = clock.now()
    inv = await db.scalar(select(Invitation).where(Invitation.token_hash == sha256_hex(token)).with_for_update())
    if inv is None or inv.accepted_at or inv.revoked_at or inv.expires_at <= now:
        raise NotFound("this invitation is not available")
    if await db.scalar(select(User.id).where(User.email == inv.email)):
        raise Conflict(f"{inv.email} already has an account; sign in instead", code="already_member")
    user = User(org_id=inv.org_id, email=inv.email, phone=inv.phone, name=(name or inv.name or inv.email.split("@")[0])[:100],
                password_hash=password_hash, role=inv.role, partner_id=inv.partner_id, status="active")
    db.add(user)
    await db.flush()
    inviter = await db.get(User, inv.invited_by) if inv.invited_by else user
    for a in inv.initial_assignments:
        client = await db.get(Client, a["client_id"])
        if client is not None and client.status == "active":
            await accounts.assign_client(db, client=client, user=user, kind=a["kind"], by=inviter or user, now=now)
    inv.accepted_at, inv.accepted_user_id = now, user.id
    return user
