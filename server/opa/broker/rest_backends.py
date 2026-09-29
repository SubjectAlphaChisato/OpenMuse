"""Read-only backends for Notion and GitHub (docs/providers/notion.md, github.md). Same interface as the Google backend."""
from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from opa.broker.google.http import hx as httpx
from opa.broker.tokens import TokenManager
from opa.sdk.contract import ConnectorError

TEXT_LIMIT = 50_000


class RestBackend:
    label = "the provider"

    def __init__(self, settings, sm, vault, connection_id, clock, oauth, transport=None, on_reauth=None) -> None:
        self.s, self.transport = settings, transport
        self.tokens = TokenManager(sm, vault, clock, connection_id, oauth, label=self.label, on_reauth=on_reauth)

    def headers(self, token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {token}"}

    async def call(self, method: str, url: str, **kw) -> Any:
        token = await self.tokens.access_token()
        async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(30, connect=10)) as c:
            for attempt in range(2):
                try:
                    r = await c.request(method, url, headers=self.headers(token), **kw)
                except httpx.TransportError:
                    raise ConnectorError("provider_unavailable", f"could not reach {self.label}") from None
                if r.status_code == 401 and attempt == 0:
                    token = await self.tokens.access_token(stale=token)  # rejected before processing: refresh and retry once
                    continue
                return self.check(r)
        raise AssertionError("unreachable")

    def check(self, r) -> Any:
        if r.status_code in (200, 201):
            return r.json()
        retry = r.headers.get("retry-after")
        if r.status_code == 429:
            raise ConnectorError("rate_limited", f"{self.label} rate limit reached", retry_after_seconds=int(retry) if retry and retry.isdigit() else None)
        if r.status_code == 401:
            raise ConnectorError("auth_required", f"{self.label} rejected the access token")
        if r.status_code == 403:
            raise ConnectorError("permission_denied", f"{self.label} denied access; the item may not be shared with this app")
        if r.status_code == 404:
            raise ConnectorError("not_found", "not found, or not shared with this app")
        if r.status_code >= 500:
            raise ConnectorError("provider_unavailable", f"{self.label} is unavailable ({r.status_code})")
        raise ConnectorError("invalid_input", f"{self.label} rejected the request ({r.status_code})")


# ------------------------------------------------------------------ Notion

def _plain(rich: list[dict[str, Any]] | None) -> str:
    return "".join(x.get("plain_text", "") for x in rich or [])


def notion_title(obj: dict[str, Any]) -> str:
    if isinstance(obj.get("title"), list):  # data sources carry a top-level title
        return _plain(obj["title"])
    for prop in (obj.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _plain(prop.get("title"))
    return ""


def notion_property_text(prop: dict[str, Any]) -> str:
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return _plain(v)
    if t in ("select", "status"):
        return (v or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(x.get("name", "") for x in v or [])
    if t == "date":
        return " to ".join(x for x in ((v or {}).get("start"), (v or {}).get("end")) if x)
    if t in ("number", "checkbox", "url", "email", "phone_number"):
        return "" if v is None else str(v)
    if t == "people":
        return ", ".join(x.get("name", "") for x in v or [])
    return ""


class NotionBackend(RestBackend):
    label = "Notion"

    def headers(self, token):
        return {"authorization": f"Bearer {token}", "notion-version": self.s.notion_version, "content-type": "application/json"}

    async def read(self, tool: str, a: dict[str, Any]) -> dict[str, Any]:
        base = self.s.notion_api_base
        size = max(1, min(a.get("page_size") or 20, 50))
        if tool == "notion.search":
            body: dict[str, Any] = {"query": a["query"], "page_size": size}
            if a.get("kind"):
                body["filter"] = {"property": "object", "value": a["kind"]}
            if a.get("start_cursor"):
                body["start_cursor"] = a["start_cursor"]
            data = await self.call("POST", f"{base}/search", json=body)
            items = [{"id": r["id"], "kind": r.get("object"), "title": notion_title(r), "url": r.get("url"), "last_edited_time": r.get("last_edited_time")}
                     for r in data.get("results", [])]
            return {"data": {"results": items, "next_cursor": data.get("next_cursor") if data.get("has_more") else None},
                    "source_refs": [f"notion:{i['id']}" for i in items]}
        if tool == "notion.read_page":
            page = await self.call("GET", f"{base}/pages/{quote(a['page_id'], safe='')}")
            lines: list[str] = []
            budget = {"blocks": 400}

            async def walk(block_id: str, depth: int) -> None:
                cursor = None
                while budget["blocks"] > 0:
                    params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
                    data = await self.call("GET", f"{base}/blocks/{quote(block_id, safe='')}/children", params=params)
                    for b in data.get("results", []):
                        budget["blocks"] -= 1
                        text = _plain((b.get(b.get("type"), {}) or {}).get("rich_text"))
                        if text:
                            lines.append("  " * depth + ("- " if "list_item" in b.get("type", "") else "") + text)
                        if b.get("has_children") and depth < 2 and budget["blocks"] > 0:
                            await walk(b["id"], depth + 1)
                        if budget["blocks"] <= 0:
                            return
                    if not data.get("has_more"):
                        return
                    cursor = data.get("next_cursor")

            await walk(a["page_id"], 0)
            text = "\n".join(lines)
            return {"data": {"page_id": page["id"], "title": notion_title(page), "url": page.get("url"), "text": text[:TEXT_LIMIT],
                             "truncated": len(text) > TEXT_LIMIT or budget["blocks"] <= 0}, "source_refs": [f"notion:{page['id']}"]}
        if tool == "notion.query_data_source":
            body = {"page_size": size, **({"start_cursor": a["start_cursor"]} if a.get("start_cursor") else {})}
            data = await self.call("POST", f"{base}/data_sources/{quote(a['data_source_id'], safe='')}/query", json=body)
            rows = [{"id": r["id"], "url": r.get("url"), "properties": {k: notion_property_text(v) for k, v in (r.get("properties") or {}).items()}}
                    for r in data.get("results", [])]
            return {"data": {"rows": rows, "next_cursor": data.get("next_cursor") if data.get("has_more") else None},
                    "source_refs": [f"notion:{r['id']}" for r in rows]}
        raise ConnectorError("invalid_input", f"unsupported read {tool}")


# ------------------------------------------------------------------ GitHub

class GitHubBackend(RestBackend):
    label = "GitHub"

    def headers(self, token):
        return {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28"}

    def check(self, r):
        if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
            reset = r.headers.get("retry-after")
            raise ConnectorError("rate_limited", "GitHub rate limit reached", retry_after_seconds=int(reset) if reset and reset.isdigit() else None)
        return super().check(r)

    async def read(self, tool: str, a: dict[str, Any]) -> dict[str, Any]:
        base = self.s.github_api_base
        if tool == "github.list_repos":
            data = await self.call("GET", f"{base}/user/repos", params={"per_page": 50, "page": max(1, a.get("page") or 1), "sort": "updated"})
            repos = [{"full_name": r["full_name"], "private": r.get("private"), "description": r.get("description"), "default_branch": r.get("default_branch"),
                      "updated_at": r.get("updated_at"), "url": r.get("html_url")} for r in data]
            return {"data": {"repos": repos}, "source_refs": [f"github:{r['full_name']}" for r in repos]}
        if tool == "github.search_issues":
            data = await self.call("GET", f"{base}/search/issues", params={"q": a["query"], "per_page": max(1, min(a.get("max_results") or 20, 50))})
            items = [{"title": i["title"], "number": i["number"], "state": i.get("state"), "repo": i.get("repository_url", "").split("/repos/")[-1],
                      "is_pull_request": "pull_request" in i, "url": i.get("html_url"), "updated_at": i.get("updated_at")} for i in data.get("items", [])]
            return {"data": {"total": data.get("total_count"), "items": items}, "source_refs": [f"github:{i['repo']}#{i['number']}" for i in items]}
        if tool == "github.get_issue":
            path = f"{base}/repos/{quote(a['owner'])}/{quote(a['repo'])}/issues/{int(a['number'])}"
            issue = await self.call("GET", path)
            comments = await self.call("GET", f"{path}/comments", params={"per_page": 30})
            ref = f"github:{a['owner']}/{a['repo']}#{a['number']}"
            return {"data": {"title": issue["title"], "state": issue.get("state"), "author": (issue.get("user") or {}).get("login"),
                             "body": (issue.get("body") or "")[:TEXT_LIMIT], "is_pull_request": "pull_request" in issue, "url": issue.get("html_url"),
                             "comments": [{"author": (c.get("user") or {}).get("login"), "body": (c.get("body") or "")[:5000], "created_at": c.get("created_at")} for c in comments]},
                    "source_refs": [ref]}
        if tool == "github.read_file":
            params = {"ref": a["ref"]} if a.get("ref") else None
            data = await self.call("GET", f"{base}/repos/{quote(a['owner'])}/{quote(a['repo'])}/contents/{quote(a['path'])}", params=params)
            if isinstance(data, list) or data.get("type") != "file":
                raise ConnectorError("invalid_input", "that path is a directory, not a file")
            raw = base64.b64decode(data.get("content") or "")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ConnectorError("invalid_input", "unsupported_format: the file is not text") from None
            return {"data": {"path": data["path"], "sha": data.get("sha"), "size": data.get("size"), "text": text[:TEXT_LIMIT],
                             "truncated": len(text) > TEXT_LIMIT}, "source_refs": [f"github:{a['owner']}/{a['repo']}/{data['path']}"]}
        raise ConnectorError("invalid_input", f"unsupported read {tool}")
