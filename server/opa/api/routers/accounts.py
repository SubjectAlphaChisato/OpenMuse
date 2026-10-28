"""CRM accounts API: team, clients, partners, account requests, and invitations (docs/crm/accounts-schema.md)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from opa.api.auth import hash_password
from opa.api.deps import AdminDep, ClockDep, DbDep, OwnerDep, SettingsDep, StaffDep
from opa.core.errors import Invalid
from opa.crm import account_requests as reqs
from opa.crm.accounts import assign_client, visible_client_ids
from opa.crm.account_requests import AccountRequestOut
from opa.db.models import AccountRequest, Client, ClientAssignment, Partner, User

router = APIRouter(prefix="/api", tags=["accounts"])


class MemberOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    status: str
    partner_id: str | None
    clients: int  # active assignments
    last_login_at: datetime | None


class ClientOut(BaseModel):
    id: str
    name: str
    status: str
    primary_advisor: str | None  # email
    my_access: Literal["primary", "collaborator", "admin"] | None


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class PartnerOut(BaseModel):
    id: str
    name: str
    status: str
    users: int


class PartnerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class RequestIn(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=300)


class RequestResult(BaseModel):
    request: AccountRequestOut
    invite_url: str | None = None  # returned once; never stored


class InvitationPreview(BaseModel):
    email: str
    name: str
    role: str
    organization: str
    expires_at: datetime


class AcceptIn(BaseModel):
    token: str = Field(min_length=20, max_length=100)
    password: str = Field(min_length=12, max_length=200)
    name: str | None = Field(default=None, max_length=100)


# ------------------------------------------------------------------ team


@router.get("/team", response_model=list[MemberOut])
async def team(db: DbDep, me: StaffDep) -> list[MemberOut]:
    users = list(await db.scalars(select(User).where(User.org_id == me.org_id).order_by(User.created_at)))
    counts: dict[str, int] = {}
    rows = await db.execute(select(ClientAssignment.user_id).where(ClientAssignment.ended_at.is_(None)))
    for (uid,) in rows:
        counts[uid] = counts.get(uid, 0) + 1
    return [MemberOut(id=u.id, email=u.email, name=u.name, role=u.role, status=u.status, partner_id=u.partner_id, clients=counts.get(u.id, 0),
                      last_login_at=u.last_login_at) for u in users]


# ------------------------------------------------------------------ clients and partners (minimal, phase 1)


@router.get("/clients", response_model=list[ClientOut])
async def list_clients(db: DbDep, me: OwnerDep) -> list[ClientOut]:
    ids = await visible_client_ids(db, me)
    q = select(Client).where(Client.org_id == me.org_id, Client.status == "active").order_by(Client.name)
    if ids is not None:
        q = q.where(Client.id.in_(ids))
    clients = list(await db.scalars(q))
    active = list(await db.scalars(select(ClientAssignment).where(ClientAssignment.client_id.in_([c.id for c in clients]), ClientAssignment.ended_at.is_(None)))) if clients else []
    emails = {u.id: u.email for u in await db.scalars(select(User).where(User.id.in_({a.user_id for a in active})))} if active else {}
    out = []
    for c in clients:
        mine = next((a for a in active if a.client_id == c.id and a.user_id == me.id), None)
        primary = next((a for a in active if a.client_id == c.id and a.kind == "primary"), None)
        out.append(ClientOut(id=c.id, name=c.name, status=c.status, primary_advisor=emails.get(primary.user_id) if primary else None,
                             my_access=mine.kind if mine else "admin" if me.role == "admin" else None))
    return out


@router.post("/clients", response_model=ClientOut, status_code=201)
async def create_client(body: ClientIn, db: DbDep, me: StaffDep, clock: ClockDep) -> ClientOut:
    """A new client belongs to the organization; the advisor who adds it becomes its primary advisor."""
    c = Client(org_id=me.org_id, name=body.name.strip())
    db.add(c)
    await db.flush()
    await assign_client(db, client=c, user=me, kind="primary", by=me, now=clock.now())
    return ClientOut(id=c.id, name=c.name, status=c.status, primary_advisor=me.email, my_access="primary")


@router.get("/partners", response_model=list[PartnerOut])
async def list_partners(db: DbDep, me: OwnerDep) -> list[PartnerOut]:
    q = select(Partner).where(Partner.org_id == me.org_id).order_by(Partner.name)
    if me.role == "partner":
        q = q.where(Partner.id == me.partner_id)  # a partner user sees only their own firm
    partners = list(await db.scalars(q))
    counts: dict[str, int] = {}
    for (pid,) in await db.execute(select(User.partner_id).where(User.partner_id.is_not(None), User.status == "active")):
        counts[pid] = counts.get(pid, 0) + 1
    return [PartnerOut(id=p.id, name=p.name, status=p.status, users=counts.get(p.id, 0)) for p in partners]


@router.post("/partners", response_model=PartnerOut, status_code=201)
async def create_partner(body: PartnerIn, db: DbDep, me: AdminDep) -> PartnerOut:
    p = Partner(org_id=me.org_id, name=body.name.strip())
    db.add(p)
    await db.flush()
    return PartnerOut(id=p.id, name=p.name, status=p.status, users=0)


# ------------------------------------------------------------------ account requests


@router.get("/account-requests", response_model=list[AccountRequestOut])
async def list_requests(db: DbDep, me: StaffDep, clock: ClockDep, status: str = "pending") -> list[AccountRequestOut]:
    q = select(AccountRequest).where(AccountRequest.org_id == me.org_id).order_by(AccountRequest.created_at.desc())
    if status != "all":
        q = q.where(AccountRequest.status == status)
    if me.role != "admin":
        q = q.where(AccountRequest.requested_by == me.id)
    rows = list(await db.scalars(q))
    cards = await reqs.describe_many(db, [r.id for r in rows], clock.now())
    return [cards[r.id] for r in rows]


@router.post("/account-requests", response_model=RequestResult, status_code=201)
async def create_request(body: RequestIn, db: DbDep, me: StaffDep, clock: ClockDep) -> RequestResult:
    """Ask for an account change. An admin's reduction applies at once; everything else waits for an admin."""
    req, url = await reqs.create(db, clock, requester=me, action=body.action, payload=body.payload, reason=body.reason)
    return RequestResult(request=reqs.describe(req, me.email, clock.now()), invite_url=url)


@router.post("/account-requests/{request_id}/confirm", response_model=RequestResult)
async def confirm_request(request_id: str, db: DbDep, me: AdminDep, clock: ClockDep, settings: SettingsDep) -> RequestResult:
    """Only this authenticated admin call adds access. The invitation link, if any, is returned here once."""
    req, url = await reqs.confirm(db, request_id, me, clock, settings)
    requester = await db.get(User, req.requested_by)
    return RequestResult(request=reqs.describe(req, requester.email if requester else "", clock.now()), invite_url=url)


@router.post("/account-requests/{request_id}/decline", response_model=AccountRequestOut)
async def decline_request(request_id: str, db: DbDep, me: StaffDep, clock: ClockDep) -> AccountRequestOut:
    req = await reqs.decline(db, request_id, me, clock)
    requester = await db.get(User, req.requested_by)
    return reqs.describe(req, requester.email if requester else "", clock.now())


# ------------------------------------------------------------------ invitations (no sign-in: the invitee has no account yet)


@router.get("/invitations/{token}", response_model=InvitationPreview)
async def preview_invitation(token: str, db: DbDep, clock: ClockDep) -> InvitationPreview:
    if not (20 <= len(token) <= 100):
        raise Invalid("invalid invitation")
    inv = await reqs.preview_invitation(db, token, clock.now())
    from opa.db.models import Organization

    org = await db.get(Organization, inv.org_id)
    return InvitationPreview(email=inv.email, name=inv.name, role=inv.role, organization=org.name if org else "", expires_at=inv.expires_at)


@router.post("/invitations/accept", status_code=201)
async def accept_invitation(body: AcceptIn, db: DbDep, clock: ClockDep) -> dict[str, str]:
    """Creates the account and spends the link. The new member then signs in with the password they chose."""
    user = await reqs.accept_invitation(db, clock, token=body.token, password_hash=hash_password(body.password), name=body.name)
    return {"email": user.email, "role": user.role}

