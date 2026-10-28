"""CRM accounts step 2: invitations and account requests through the API.

Adding access waits for an admin's click; an admin's reduction applies at once; advisors can ask but never
confirm; partner users can do neither; suspended and deactivated accounts are signed out immediately and
cannot sign in; invitation links are single-use, expire, and are never stored.
"""
from contextlib import asynccontextmanager

from httpx import ASGITransport
from sqlalchemy import select, text

from opa.api.auth import hash_password
from opa.core.canonical import sha256_hex
from opa.db.models import DEFAULT_ORG_ID, Client, Invitation, Partner, User
from tests.conftest import Client as Http

PW = "a long enough password"


async def member(sessionmaker, email, role="advisor", **kw) -> str:
    async with sessionmaker() as db, db.begin():
        u = User(email=email, name=email.split("@")[0], role=role, password_hash=hash_password(PW), **kw)
        db.add(u)
        await db.flush()
        return u.id


@asynccontextmanager
async def http(app, email=None):
    async with Http(transport=ASGITransport(app=app), base_url="http://test") as c:
        if email:
            await c.sign_in(email, PW)
        yield c


async def test_an_advisors_invitation_needs_an_admin_and_the_link_works_once(client, app, sessionmaker, clock):
    """`client` is signed in as the bootstrap owner, who is now the admin."""
    await member(sessionmaker, "a1@example.com")
    zhang = (await client.post("/api/clients", json={"name": "张总"})).json()
    async with http(app, "a1@example.com") as a1:
        r = await a1.post("/api/account-requests", json={"action": "invite", "reason": "新同事", "payload": {
            "email": "New@Example.com", "name": "小王", "role": "advisor", "initial_assignments": [{"client_name": "张总", "kind": "collaborator"}]}})
        assert r.status_code == 201, r.text
        req = r.json()["request"]
        assert req["status"] == "pending" and req["requires_admin"] and r.json()["invite_url"] is None
        assert req["lines"] == ["邮箱：new@example.com", "角色：顾问", "客户：张总（协作）"]
        assert (await a1.post(f"/api/account-requests/{req['id']}/confirm")).status_code == 403  # advisors never confirm
    assert (await client.get("/api/invitations/not-a-real-token-at-all")).status_code == 404
    assert [x["id"] for x in (await client.get("/api/account-requests")).json()] == [req["id"]]

    r = await client.post(f"/api/account-requests/{req['id']}/confirm")
    assert r.status_code == 200, r.text
    url = r.json()["invite_url"]
    assert url.startswith("http://127.0.0.1:8080/invite/") and r.json()["request"]["status"] == "completed"
    token = url.rsplit("/", 1)[1]
    async with sessionmaker() as db:
        inv = (await db.scalars(select(Invitation))).one()
        assert inv.token_hash == sha256_hex(token) and token not in str((await db.execute(text("SELECT row_to_json(i)::text FROM invitations i"))).scalar())
    assert (await client.post(f"/api/account-requests/{req['id']}/confirm")).status_code == 409  # single use

    async with http(app) as anon:
        preview = await anon.get(f"/api/invitations/{token}")
        assert preview.status_code == 200 and preview.json()["email"] == "new@example.com" and preview.json()["organization"] == "Default"
        assert (await anon.post("/api/invitations/accept", json={"token": token, "password": "short"})).status_code == 422
        r = await anon.post("/api/invitations/accept", json={"token": token, "password": PW})
        assert r.status_code == 201 and r.json() == {"email": "new@example.com", "role": "advisor"}
        assert (await anon.post("/api/invitations/accept", json={"token": token, "password": PW})).status_code == 404  # spent
    async with http(app, "new@example.com") as new:
        me = (await new.get("/api/me")).json()
        assert (me["role"], me["name"]) == ("advisor", "小王")
        assert [(c["name"], c["my_access"]) for c in (await new.get("/api/clients")).json()] == [("张总", "collaborator")]
        assert (await new.get("/api/team")).status_code == 200
    assert next(m for m in (await client.get("/api/team")).json() if m["email"] == "new@example.com")["clients"] == 1
    assert zhang["primary_advisor"] == "owner@example.com"


async def test_expired_invitations_and_existing_members_are_refused(client, app, sessionmaker, clock):
    r = await client.post("/api/account-requests", json={"action": "invite", "payload": {"email": "late@example.com", "role": "advisor"}})
    token = (await client.post(f"/api/account-requests/{r.json()['request']['id']}/confirm")).json()["invite_url"].rsplit("/", 1)[1]
    clock.advance(days=7, seconds=1)
    async with http(app) as anon:
        assert (await anon.post("/api/invitations/accept", json={"token": token, "password": PW})).status_code == 404
    dup = await client.post("/api/account-requests", json={"action": "invite", "payload": {"email": "owner@example.com", "role": "advisor"}})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "already_member"
    bad = await client.post("/api/account-requests", json={"action": "invite", "payload": {"email": "p@example.com", "role": "partner"}})
    assert bad.status_code == 422  # a partner invitation needs a partner firm


async def test_admin_widening_waits_for_the_click_but_reductions_apply_at_once(client, app, sessionmaker, clock):
    a1 = await member(sessionmaker, "a1@example.com")
    a2 = await member(sessionmaker, "a2@example.com")
    zhang = (await client.post("/api/clients", json={"name": "张总"})).json()
    r = await client.post("/api/account-requests", json={"action": "assign_client", "payload": {"client_id": zhang["id"], "user_email": "a1@example.com", "kind": "collaborator"}})
    assert r.json()["request"]["status"] == "pending"  # the admin asked, but adding access still needs the click
    async with http(app, "a1@example.com") as http_a1:
        assert (await http_a1.get("/api/clients")).json() == []
        assert (await client.post(f"/api/account-requests/{r.json()['request']['id']}/confirm")).status_code == 200
        assert [c["my_access"] for c in (await http_a1.get("/api/clients")).json()] == ["collaborator"]
        # transfer: a1 becomes primary; the owner (old primary) is ended
        t = await client.post("/api/account-requests", json={"action": "transfer_client", "payload": {"client_name": "张总", "user_id": a1}})
        assert t.json()["request"]["lines"] == ["主责顾问：owner@example.com → a1@example.com"]
        await client.post(f"/api/account-requests/{t.json()['request']['id']}/confirm")
        assert [c["my_access"] for c in (await http_a1.get("/api/clients")).json()] == ["primary"]
        # reduction by an admin: immediate
        u = await client.post("/api/account-requests", json={"action": "unassign_client", "payload": {"client_id": zhang["id"], "user_id": a1}})
        assert u.status_code == 422 and u.json()["error"]["code"] == "transfer_instead"  # a client keeps a primary advisor
        await client.post(f"/api/account-requests/{(await client.post('/api/account-requests', json={'action': 'assign_client', 'payload': {'client_id': zhang['id'], 'user_id': a2, 'kind': 'collaborator'}})).json()['request']['id']}/confirm")
        u = await client.post("/api/account-requests", json={"action": "unassign_client", "payload": {"client_id": zhang["id"], "user_id": a2}})
        assert u.json()["request"]["status"] == "completed" and u.json()["request"]["requires_admin"] is False
    async with http(app, "a2@example.com") as http_a2:
        assert (await http_a2.get("/api/clients")).json() == []


async def test_suspend_signs_out_at_once_and_reactivation_needs_the_click(client, app, sessionmaker, clock):
    await member(sessionmaker, "a1@example.com")
    async with http(app, "a1@example.com") as a1:
        assert (await a1.get("/api/me")).status_code == 200
        s = await client.post("/api/account-requests", json={"action": "suspend", "payload": {"user_email": "a1@example.com"}, "reason": "休假"})
        assert s.status_code == 201 and s.json()["request"]["status"] == "completed"
        assert (await a1.get("/api/me")).status_code == 401  # signed out everywhere, at once
        login = await a1.post("/api/auth/login", json={"email": "a1@example.com", "password": PW})
        assert login.status_code == 401 and "suspended" in login.json()["error"]["message"]
    r = await client.post("/api/account-requests", json={"action": "reactivate", "payload": {"user_email": "a1@example.com"}})
    assert r.json()["request"]["status"] == "pending"  # giving access back needs the click
    await client.post(f"/api/account-requests/{r.json()['request']['id']}/confirm")
    async with http(app, "a1@example.com") as a1:
        assert (await a1.get("/api/me")).status_code == 200
    me_suspend = await client.post("/api/account-requests", json={"action": "suspend", "payload": {"user_email": "owner@example.com"}})
    assert me_suspend.status_code == 422


async def test_deactivation_and_role_changes_keep_the_rules(client, app, sessionmaker, clock):
    a1 = await member(sessionmaker, "a1@example.com")
    async with sessionmaker() as db, db.begin():
        firm = Partner(org_id=DEFAULT_ORG_ID, name="XX 资产")
        db.add(firm)
        await db.flush()
        firm_id = firm.id
    zhang = (await client.post("/api/clients", json={"name": "张总"})).json()
    t = await client.post("/api/account-requests", json={"action": "transfer_client", "payload": {"client_id": zhang["id"], "user_id": a1}})
    await client.post(f"/api/account-requests/{t.json()['request']['id']}/confirm")
    d = await client.post("/api/account-requests", json={"action": "deactivate", "payload": {"user_id": a1}})
    assert d.status_code == 409 and d.json()["error"]["code"] == "clients_not_transferred"
    async with sessionmaker() as db:  # a refused change rolls back: nothing is recorded as pending or done
        assert [r["action"] for r in (await client.get("/api/account-requests?status=all")).json()] == ["transfer_client"]
    demote = await client.post("/api/account-requests", json={"action": "change_role", "payload": {"user_email": "owner@example.com", "role": "advisor"}})
    assert (await client.post(f"/api/account-requests/{demote.json()['request']['id']}/confirm")).json()["error"]["code"] == "last_admin"
    to_partner = await client.post("/api/account-requests", json={"action": "change_role", "payload": {"user_id": a1, "role": "partner", "partner_id": firm_id}})
    assert (await client.post(f"/api/account-requests/{to_partner.json()['request']['id']}/confirm")).json()["error"]["code"] == "clients_not_transferred"
    back = await client.post("/api/account-requests", json={"action": "transfer_client", "payload": {"client_id": zhang["id"], "user_email": "owner@example.com"}})
    await client.post(f"/api/account-requests/{back.json()['request']['id']}/confirm")
    to_partner = await client.post("/api/account-requests", json={"action": "change_role", "payload": {"user_id": a1, "role": "partner", "partner_id": firm_id}})
    assert (await client.post(f"/api/account-requests/{to_partner.json()['request']['id']}/confirm")).json()["request"]["status"] == "completed"
    async with http(app, "a1@example.com") as p:  # now a partner user: sees only their firm, cannot ask for account changes
        assert [x["name"] for x in (await p.get("/api/partners")).json()] == ["XX 资产"]
        assert (await p.get("/api/team")).status_code == 403
        assert (await p.post("/api/account-requests", json={"action": "invite", "payload": {"email": "x@example.com", "role": "advisor"}})).status_code == 403
    d = await client.post("/api/account-requests", json={"action": "deactivate", "payload": {"user_id": a1}})
    assert d.json()["request"]["status"] == "completed"
    async with sessionmaker() as db:
        u = await db.get(User, a1)
        assert (u.status, u.role) == ("deactivated", "partner") and await db.scalar(select(Client.id)) is not None


async def test_requests_expire_and_newer_ones_replace_older(client, sessionmaker, clock):
    a1 = await member(sessionmaker, "a1@example.com")
    first = (await client.post("/api/account-requests", json={"action": "change_role", "payload": {"user_id": a1, "role": "admin"}})).json()["request"]
    second = (await client.post("/api/account-requests", json={"action": "change_role", "payload": {"user_id": a1, "role": "admin"}})).json()["request"]
    statuses = {r["id"]: r["status"] for r in (await client.get("/api/account-requests?status=all")).json()}
    assert statuses[first["id"]] == "superseded" and statuses[second["id"]] == "pending"
    clock.advance(hours=24, seconds=1)
    r = await client.post(f"/api/account-requests/{second['id']}/confirm")
    assert r.status_code == 409 and r.json()["error"]["code"] == "expired"
    async with sessionmaker() as db:
        assert (await db.get(User, a1)).role == "advisor"


async def test_an_advisors_reduction_also_waits_for_an_admin(client, app, sessionmaker, clock):
    await member(sessionmaker, "a1@example.com")
    await member(sessionmaker, "a2@example.com")
    async with http(app, "a1@example.com") as a1, http(app, "a2@example.com") as a2:
        r = await a1.post("/api/account-requests", json={"action": "suspend", "payload": {"user_email": "a2@example.com"}})
        assert r.status_code == 201 and r.json()["request"]["status"] == "pending"  # only an admin's reduction is immediate
        assert (await a2.get("/api/me")).status_code == 200


async def test_service_guards_hold_even_without_the_route_guards(client, sessionmaker, clock, settings):
    """The API routes refuse these first; the service refuses them again, so no future caller can skip the rule."""
    from opa.core.errors import Forbidden
    from opa.crm import account_requests as reqs

    a1 = await member(sessionmaker, "a1@example.com")
    async with sessionmaker() as db, db.begin():
        firm = Partner(org_id=DEFAULT_ORG_ID, name="XX 资产")
        db.add(firm)
        await db.flush()
        p = User(email="p@example.com", name="p", role="partner", partner_id=firm.id, password_hash=hash_password(PW))
        db.add(p)
        await db.flush()
        admin = await db.scalar(select(User).where(User.email == "owner@example.com"))
        advisor = await db.get(User, a1)
        req, _ = await reqs.create(db, clock, requester=admin, action="change_role", payload={"user_id": a1, "role": "admin"})
        try:
            await reqs.confirm(db, req.id, advisor, clock, settings)
            raise AssertionError("an advisor confirmed")
        except Forbidden:
            pass
        try:
            await reqs.create(db, clock, requester=p, action="invite", payload={"email": "x@example.com", "role": "advisor"})
            raise AssertionError("a partner user created a request")
        except Forbidden:
            pass
        assert (await db.get(User, a1)).role == "advisor"


async def test_a_session_dies_with_the_account_status_even_if_nothing_deleted_it(client, app, sessionmaker):
    a1 = await member(sessionmaker, "a1@example.com")
    async with http(app, "a1@example.com") as http_a1:
        assert (await http_a1.get("/api/me")).status_code == 200
        async with sessionmaker() as db, db.begin():
            (await db.get(User, a1)).status = "suspended"  # status only; the session row still exists
        assert (await http_a1.get("/api/me")).status_code == 401
