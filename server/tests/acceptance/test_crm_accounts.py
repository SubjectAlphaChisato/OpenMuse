"""CRM phase 1: organizations, users with roles, and client assignments (docs/crm/accounts-schema.md).

The rules checked here are the ones the design calls "必须成立": a partner user belongs to a partner firm and
nobody else does; one primary advisor per client; advisors see only their clients; a deactivated user is signed
out at once, cannot still hold clients, and the last admin cannot go.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from opa.api.auth import hash_password
from opa.core.errors import Conflict, Invalid
from opa.crm.accounts import assign_client, deactivate_user, end_assignment, visible_client_ids
from opa.db.models import DEFAULT_ORG_ID, Client, ClientAssignment, Organization, Partner, User


async def add(db, **kw) -> User:
    u = User(password_hash=hash_password("a long enough password"), **kw)
    db.add(u)
    await db.flush()
    return u


async def seed(sessionmaker):
    async with sessionmaker() as db, db.begin():
        admin = await add(db, email="admin@example.com", name="Admin")  # defaults: admin, active, default org
        a1 = await add(db, email="a1@example.com", name="Advisor 1", role="advisor")
        a2 = await add(db, email="a2@example.com", name="Advisor 2", role="advisor")
        firm = Partner(org_id=DEFAULT_ORG_ID, name="XX Asset Management")
        db.add(firm)
        await db.flush()
        p = await add(db, email="p@example.com", name="Partner user", role="partner", partner_id=firm.id)
        zhang, li = Client(org_id=DEFAULT_ORG_ID, name="张总"), Client(org_id=DEFAULT_ORG_ID, name="李姐")
        db.add_all([zhang, li])
        await db.flush()
        ids = dict(admin=admin.id, a1=a1.id, a2=a2.id, p=p.id, firm=firm.id, zhang=zhang.id, li=li.id)
    return ids


async def load(db, ids, *keys):
    out = []
    for k in keys:
        model = Client if k in ("zhang", "li") else Partner if k == "firm" else User
        out.append(await db.get(model, ids[k]))
    return out


async def test_existing_owner_is_the_admin_of_the_default_organization(sessionmaker, owner):
    async with sessionmaker() as db:
        u = await db.get(User, owner.id)
        org = await db.get(Organization, DEFAULT_ORG_ID)
        assert (u.role, u.status, u.org_id, org.name) == ("admin", "active", DEFAULT_ORG_ID, "Default")


async def test_only_partner_users_belong_to_a_partner_firm(sessionmaker):
    ids = await seed(sessionmaker)
    for kw in (dict(role="partner", partner_id=None), dict(role="advisor", partner_id=ids["firm"]), dict(role="owner")):
        async with sessionmaker() as db:
            with pytest.raises(IntegrityError):
                async with db.begin():
                    await add(db, email="x@example.com", name="x", **kw)


async def test_one_primary_advisor_per_client_and_transfers_keep_history(sessionmaker, clock):
    ids = await seed(sessionmaker)
    async with sessionmaker() as db, db.begin():
        admin, a1, a2, zhang = await load(db, ids, "admin", "a1", "a2", "zhang")
        first = await assign_client(db, client=zhang, user=a1, kind="primary", by=admin, now=clock.now())
        assert first.can_edit is True
        assert await assign_client(db, client=zhang, user=a1, kind="primary", by=admin, now=clock.now()) is first  # idempotent
        clock.advance(hours=1)
        second = await assign_client(db, client=zhang, user=a2, kind="primary", by=admin, now=clock.now())
        assert second.id != first.id and first.ended_at == clock.now() and first.ended_reason == "transferred to a2@example.com"
    async with sessionmaker() as db:  # the database itself refuses a second active primary
        with pytest.raises(IntegrityError):
            async with db.begin():
                db.add(ClientAssignment(org_id=DEFAULT_ORG_ID, client_id=ids["zhang"], user_id=ids["a1"], kind="primary"))
                await db.flush()


async def test_advisors_see_only_their_clients_and_admins_see_all(sessionmaker, clock):
    ids = await seed(sessionmaker)
    async with sessionmaker() as db, db.begin():
        admin, a1, a2, p, zhang, li = await load(db, ids, "admin", "a1", "a2", "p", "zhang", "li")
        await assign_client(db, client=zhang, user=a1, kind="primary", by=admin, now=clock.now())
        await assign_client(db, client=li, user=a2, kind="primary", by=admin, now=clock.now())
        collab = await assign_client(db, client=li, user=a1, kind="collaborator", by=admin, now=clock.now())
        assert collab.can_edit is False
        assert await visible_client_ids(db, admin) is None
        assert await visible_client_ids(db, a1) == sorted([zhang.id, li.id])
        assert await visible_client_ids(db, a2) == [li.id]
        assert await visible_client_ids(db, p) == []  # partner users never see clients
        await end_assignment(db, collab, reason="no longer needed", now=clock.now())
        assert await visible_client_ids(db, a1) == [zhang.id]  # reductions apply at once
        with pytest.raises(Invalid):
            await assign_client(db, client=zhang, user=p, kind="collaborator", by=admin, now=clock.now())


async def test_deactivation_signs_out_refuses_held_clients_and_keeps_an_admin(sessionmaker, clock, app):
    from httpx import ASGITransport
    from tests.conftest import Client as HttpClient

    ids = await seed(sessionmaker)
    async with HttpClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await http.sign_in("a1@example.com", "a long enough password")
        assert (await http.get("/api/me")).status_code == 200
        async with sessionmaker() as db, db.begin():
            admin, a1, zhang = await load(db, ids, "admin", "a1", "zhang")
            await assign_client(db, client=zhang, user=a1, kind="primary", by=admin, now=clock.now())
            with pytest.raises(Conflict, match="still holds 1 client"):
                await deactivate_user(db, a1, by=admin, now=clock.now())
            with pytest.raises(Conflict, match="at least one active admin"):
                await deactivate_user(db, admin, by=admin, now=clock.now())
        async with sessionmaker() as db, db.begin():
            admin, a1, a2, zhang = await load(db, ids, "admin", "a1", "a2", "zhang")
            await assign_client(db, client=zhang, user=a2, kind="primary", by=admin, now=clock.now())  # transfer first
            await deactivate_user(db, a1, by=admin, now=clock.now())
        assert (await http.get("/api/me")).status_code == 401  # signed out everywhere, at once
        async with sessionmaker() as db:
            a1 = await db.get(User, ids["a1"])
            assert (a1.status, a1.deactivated_by) == ("deactivated", ids["admin"])
            assert await visible_client_ids(db, a1) == []
            assert (await db.scalar(select(ClientAssignment.user_id).where(ClientAssignment.client_id == ids["zhang"], ClientAssignment.ended_at.is_(None)))) == ids["a2"]
