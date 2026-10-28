"""Release gates (spec section 11) checked mechanically.

* every public API route rejects anonymous callers, and every state-changing route requires the CSRF token;
* the internal broker and browser services reject calls without the internal token;
* no approval bypass: only the owner's decision endpoint can mark an action or approval "approved",
  which also means nothing can move an unknown or dispatching write back to a sendable state.
"""
import ast
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from opa.broker.app import create_broker_app

OPA = Path(__file__).resolve().parents[1] / "opa"
PUBLIC = {("POST", "/api/auth/login"), ("GET", "/api/oauth/google/callback"),  # the callback still requires a session (checked below)
          ("GET", "/api/invitations/{token}"), ("POST", "/api/invitations/accept")}  # CRM: the invitee has no account yet
ANON_OK = {("GET", "/api/invitations/{token}"): (404,), ("POST", "/api/invitations/accept"): (422,)}  # never 200 without a valid link


def _routes(app):
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            yield method.upper(), path, re.sub(r"\{[^}]+\}", "01ZZZZZZZZZZZZZZZZZZZZZZZZ", path)


async def test_every_route_rejects_anonymous_callers(app, anon, owner):
    checked = 0
    for method, path, concrete in _routes(app):
        if (method, path) == ("POST", "/api/auth/login"):
            continue
        r = await anon.request(method, concrete, json={})
        if (method, path) == ("GET", "/api/oauth/google/callback"):
            assert r.status_code in (401, 303), (method, path)
            continue
        if (method, path) in ANON_OK:
            assert r.status_code in ANON_OK[(method, path)], (method, path, r.status_code)
            continue
        assert r.status_code == 401, (method, path, r.status_code)
        checked += 1
    assert checked >= 50


async def test_every_state_changing_route_requires_csrf(app, client):
    del client.headers["x-csrf-token"]
    checked = 0
    for method, path, concrete in _routes(app):
        if method in ("GET", "HEAD") or (method, path) in PUBLIC:
            continue
        r = await client.request(method, concrete, json={})
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed", (method, path, r.status_code)
        checked += 1
    assert checked >= 25


async def test_internal_services_require_the_internal_token(broker):
    from opa.browser.app import create_browser_app
    from opa.browser.service import BrowserService

    for app_ in (create_broker_app(broker), create_browser_app(BrowserService("http://127.0.0.1:1"), "token")):
        async with AsyncClient(transport=ASGITransport(app=app_), base_url="http://internal") as c:
            for route in app_.routes:
                path = getattr(route, "path", "")
                if not path.startswith("/internal/") or path == "/internal/health":
                    continue
                for method in getattr(route, "methods", []) or []:
                    r = await c.request(method, path.replace("{sid}", "x"), json={})
                    assert r.status_code == 401, (method, path)


def _approved_assignments() -> list[str]:
    """Every place in the code that sets a state to "approved"."""
    hits = []
    for file in OPA.rglob("*.py"):
        tree = ast.parse(file.read_text())
        func_of = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    func_of.setdefault(id(node), fn.name)
        for node in ast.walk(tree):
            value = None
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "state" for t in node.targets):
                value = node.value
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Tuple) and any(isinstance(e, ast.Attribute) and e.attr == "state" for e in t.elts) for t in node.targets):
                value = node.value.elts[0] if isinstance(node.value, ast.Tuple) else node.value
            elif isinstance(node, ast.keyword) and node.arg == "state":
                value = node.value
            if isinstance(value, ast.Constant) and value.value == "approved":
                hits.append(f"{file.relative_to(OPA.parent)}:{func_of.get(id(node), '<module>')}")
    for file in OPA.rglob("*.py"):  # raw SQL too
        if re.search(r"SET\s+state\s*=\s*'approved'", file.read_text(), re.I):
            hits.append(f"{file.relative_to(OPA.parent)}:<sql>")
    return sorted(set(hits))


def test_no_approval_bypass_only_the_decision_endpoint_approves():
    assert _approved_assignments() == ["opa/api/routers/approvals.py:decide"]


@pytest.mark.parametrize("path", ["opa/broker/service.py", "opa/worker/loop.py"])
def test_dispatch_paths_require_an_approved_action(path):
    src = (OPA.parent / path).read_text()
    assert 'action.state != "approved"' in src or 'Action.state == "approved"' in src
