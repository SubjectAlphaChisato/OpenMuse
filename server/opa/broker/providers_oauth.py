"""Sign-in clients per provider, with one interface the broker uses (docs/providers/*.md).

exchange(code, verifier, redirect_uri) -> token dict      refresh(refresh_token) -> token dict (InvalidGrant if revoked)
identify(token) -> {"sub", "label"}                        check(access_token) -> None (a cheap authenticated call)
revoke(secret) -> (revoked, error)                         granted_scopes(token) -> list[str]
Only the broker constructs these; client secrets never leave it.
"""
from __future__ import annotations

import base64
from typing import Any

from opa.broker.google.http import hx as httpx
from opa.broker.google.oauth import GoogleOAuth
from opa.broker.tokens import InvalidGrant
from opa.connectors import registry
from opa.sdk.contract import ConnectorError


def _need(settings, provider: str) -> tuple[str, str]:
    cid, secret = registry.client_id(settings, provider), registry.client_secret(settings, provider)
    if not cid or not secret:
        raise ConnectorError("auth_required", f"{registry.PROVIDERS[provider].label} sign-in is not configured: {registry.PROVIDERS[provider].setup}")
    return cid, secret


def _basic(cid: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()


async def _send(transport, method: str, url: str, **kw) -> Any:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=30) as c:
            return await c.request(method, url, **kw)
    except httpx.TransportError:
        raise ConnectorError("provider_unavailable", "could not reach the provider") from None


class GoogleClient:
    def __init__(self, settings, transport=None) -> None:
        self.g = GoogleOAuth(settings, transport)

    async def exchange(self, code, verifier, redirect_uri):
        return await self.g.exchange(code, verifier, redirect_uri)

    async def refresh(self, refresh_token):
        return await self.g.refresh(refresh_token)

    async def identify(self, token):
        ident = await self.g.identity(token["access_token"])
        return {"sub": ident["sub"], "label": ident["email"]}

    async def check(self, access_token):
        await self.g.identity(access_token)

    async def revoke(self, secret):
        return await self.g.revoke(secret.get("refresh_token") or secret["access_token"])

    @staticmethod
    def granted_scopes(token):
        return sorted(set((token.get("scope") or "").split()))


class NotionClient:
    """Notion public integration: Basic client auth, JSON bodies, no PKCE, workspace identity from the token response."""

    def __init__(self, settings, transport=None) -> None:
        self.s, self.transport = settings, transport
        self.cid, self.secret = _need(settings, "notion")
        self.headers = {"authorization": _basic(self.cid, self.secret), "content-type": "application/json", "notion-version": settings.notion_version}

    async def _token(self, body: dict[str, Any]) -> dict[str, Any]:
        r = await _send(self.transport, "POST", f"{self.s.notion_api_base}/oauth/token", json=body, headers=self.headers)
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("access_token"):
            return data
        if data.get("error") == "invalid_grant" and body["grant_type"] == "refresh_token":
            raise InvalidGrant()
        raise ConnectorError("auth_required", f"Notion rejected the sign-in ({data.get('error', r.status_code)})")

    async def exchange(self, code, verifier, redirect_uri):
        return await self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})

    async def refresh(self, refresh_token):
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def identify(self, token):
        if not token.get("workspace_id"):
            raise ConnectorError("auth_required", "Notion did not return a workspace")
        return {"sub": token["workspace_id"], "label": f"{token.get('workspace_name') or 'Notion workspace'} (Notion)"}

    async def check(self, access_token):
        r = await _send(self.transport, "GET", f"{self.s.notion_api_base}/users/me",
                        headers={"authorization": f"Bearer {access_token}", "notion-version": self.s.notion_version})
        if r.status_code == 401:
            raise ConnectorError("auth_required", "Notion rejected the access token")
        if r.status_code != 200:
            raise ConnectorError("provider_unavailable", f"Notion answered {r.status_code}")

    async def revoke(self, secret):
        try:
            r = await _send(self.transport, "POST", f"{self.s.notion_api_base}/oauth/revoke", json={"token": secret["access_token"]}, headers=self.headers)
        except ConnectorError:
            return False, "could not reach Notion to revoke access"
        return (True, None) if r.status_code == 200 else (False, f"Notion answered {r.status_code} to the revocation request")

    @staticmethod
    def granted_scopes(token):
        return []


class GitHubClient:
    """GitHub App user tokens: PKCE, form-encoded token requests, JSON responses, errors reported in the body."""

    def __init__(self, settings, transport=None) -> None:
        self.s, self.transport = settings, transport
        self.cid, self.secret = _need(settings, "github")

    async def _token(self, form: dict[str, Any]) -> dict[str, Any]:
        r = await _send(self.transport, "POST", self.s.github_token_url, data={"client_id": self.cid, "client_secret": self.secret, **form},
                        headers={"accept": "application/json"})
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("access_token"):
            return data
        if data.get("error") == "bad_refresh_token" and form.get("grant_type") == "refresh_token":
            raise InvalidGrant()
        raise ConnectorError("auth_required", f"GitHub rejected the sign-in ({data.get('error', r.status_code)})")

    async def exchange(self, code, verifier, redirect_uri):
        return await self._token({"code": code, "redirect_uri": redirect_uri, "code_verifier": verifier})

    async def refresh(self, refresh_token):
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _api_headers(self, access_token):
        return {"authorization": f"Bearer {access_token}", "accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"}

    async def identify(self, token):
        r = await _send(self.transport, "GET", f"{self.s.github_api_base}/user", headers=self._api_headers(token["access_token"]))
        if r.status_code != 200:
            raise ConnectorError("auth_required", "could not read the GitHub account")
        u = r.json()
        return {"sub": str(u["id"]), "label": f"{u['login']} (GitHub)"}

    async def check(self, access_token):
        r = await _send(self.transport, "GET", f"{self.s.github_api_base}/user", headers=self._api_headers(access_token))
        if r.status_code == 401:
            raise ConnectorError("auth_required", "GitHub rejected the access token")
        if r.status_code != 200:
            raise ConnectorError("provider_unavailable", f"GitHub answered {r.status_code}")

    async def revoke(self, secret):
        try:
            r = await _send(self.transport, "DELETE", f"{self.s.github_api_base}/applications/{self.cid}/token",
                            json={"access_token": secret["access_token"]},
                            headers={"authorization": _basic(self.cid, self.secret), "accept": "application/vnd.github+json"})
        except ConnectorError:
            return False, "could not reach GitHub to revoke access"
        return (True, None) if r.status_code == 204 else (False, f"GitHub answered {r.status_code} to the revocation request")

    @staticmethod
    def granted_scopes(token):
        return []


def client_for(provider: str, settings, transport=None):
    return {"google": GoogleClient, "notion": NotionClient, "github": GitHubClient}[provider](settings, transport)
