"""Google Gmail and Calendar backend (GMAIL-01, CAL-01). Same interface as the fixture backend.

Error mapping for writes is deliberately conservative: once a request may have reached Google,
any transport failure or 5xx is an unknown outcome, never a safe failure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opa.broker.google.http import hx as httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opa.broker.google import mime
from opa.broker.google.oauth import GoogleOAuth, InvalidGrant
from opa.broker.vault import Vault
from opa.core.clock import Clock
from opa.core.settings import Settings
from opa.db.models import BrokerCredential
from opa.sdk.contract import ConnectorError, OutcomeUnknown

REFRESH_MARGIN = timedelta(seconds=60)
METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date"]


class ReauthRequired(ConnectorError):
    def __init__(self) -> None:
        super().__init__("auth_required", "Google access was revoked or expired; reconnect the account")


class GoogleBackend:
    provider = "google"

    def __init__(self, settings: Settings, sm: async_sessionmaker[AsyncSession], vault: Vault, connection_id: str, clock: Clock,
                 transport: httpx.AsyncBaseTransport | None = None, on_reauth=None) -> None:
        self.s = settings
        self.sm = sm
        self.vault = vault
        self.connection_id = connection_id
        self.clock = clock
        self.transport = transport
        self.oauth = GoogleOAuth(settings, transport)
        self.on_reauth = on_reauth  # async callback(connection_id) when the grant is gone

    # ------------------------------------------------------------ tokens (single-flight refresh)
    async def access_token(self, *, stale: str | None = None) -> str:
        """Return a valid access token. Refreshes under a row lock so concurrent callers refresh once."""
        async with self.sm() as db, db.begin():
            row = await db.scalar(select(BrokerCredential).where(BrokerCredential.connection_id == self.connection_id).with_for_update())
            if row is None:
                raise ConnectorError("auth_required", "no stored Google credential; reconnect the account")
            secret = self.vault.decrypt(row.dek_wrapped, row.secret_enc, self.connection_id)
            expires = datetime.fromisoformat(secret["expires_at"])
            fresh_enough = expires - REFRESH_MARGIN > self.clock.now()
            if fresh_enough and (stale is None or secret["access_token"] != stale):
                return secret["access_token"]
            try:
                tok = await self.oauth.refresh(secret["refresh_token"])
            except InvalidGrant:
                reauth = True
            else:
                reauth = False
                secret["access_token"] = tok["access_token"]
                secret["expires_at"] = (self.clock.now() + timedelta(seconds=int(tok.get("expires_in", 3600)))).isoformat()
                if tok.get("refresh_token"):
                    secret["refresh_token"] = tok["refresh_token"]
                if tok.get("scope"):
                    secret["scopes"] = sorted(set(tok["scope"].split()))
                row.dek_wrapped, row.secret_enc = self.vault.encrypt(secret, self.connection_id)
                row.access_expires_at = datetime.fromisoformat(secret["expires_at"])
                return secret["access_token"]
        if reauth:
            if self.on_reauth:
                await self.on_reauth(self.connection_id)
            raise ReauthRequired()
        raise AssertionError("unreachable")

    # ------------------------------------------------------------ HTTP with error mapping
    async def _call(self, method: str, url: str, *, write: bool = False, params: Any = None, json: Any = None, headers: dict[str, str] | None = None) -> Any:
        token = await self.access_token()
        async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(30, connect=10)) as c:
            for attempt in range(2):
                try:
                    r = await c.request(method, url, params=params, json=json, headers={"authorization": f"Bearer {token}", **(headers or {})})
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    raise ConnectorError("provider_unavailable", "could not connect to Google") from None
                except httpx.TransportError:
                    if write:
                        raise OutcomeUnknown("the connection failed after the request was sent") from None
                    raise ConnectorError("provider_unavailable", "Google did not answer") from None
                if r.status_code == 401 and attempt == 0:
                    token = await self.access_token(stale=token)  # rejected before processing: safe to retry once
                    continue
                return self._check(r, write)
        raise AssertionError("unreachable")

    @staticmethod
    def _check(r: httpx.Response, write: bool) -> Any:
        if r.status_code in (200, 201):
            return r.json()
        if r.status_code == 204:
            return {}
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        message = err.get("message") if isinstance(err, dict) else str(err)
        reasons = {e.get("reason") for e in (err.get("errors") or [])} if isinstance(err, dict) else set()
        if r.status_code >= 500:
            if write:
                raise OutcomeUnknown(f"Google answered {r.status_code}")
            raise ConnectorError("provider_unavailable", f"Google is unavailable ({r.status_code})")
        if r.status_code == 401:
            raise ConnectorError("auth_required", "Google rejected the access token")
        if r.status_code == 429 or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
            retry = r.headers.get("retry-after")
            raise ConnectorError("rate_limited", "Google rate limit reached", retry_after_seconds=int(retry) if retry and retry.isdigit() else None)
        if r.status_code == 403:
            raise ConnectorError("permission_denied", message or "Google denied this operation; the account may not have granted the needed access")
        if r.status_code in (404, 410):
            raise ConnectorError("not_found", message or "not found")
        if r.status_code in (409, 412):
            raise ConnectorError("conflict", message or "the item changed or already exists")
        raise ConnectorError("invalid_input", message or f"Google rejected the request ({r.status_code})")

    # ------------------------------------------------------------ reads
    async def read(self, tool: str, a: dict[str, Any]) -> dict[str, Any]:
        g, cal = self.s.gmail_api_base, self.s.calendar_api_base
        if tool == "gmail.search_messages":
            size = max(1, min(a.get("max_results") or 20, 100))
            params = {"q": a["query"], "maxResults": size}
            if a.get("page_token"):
                params["pageToken"] = a["page_token"]
            listing = await self._call("GET", f"{g}/users/me/messages", params=params)
            ids = [m["id"] for m in listing.get("messages", [])]
            sem = asyncio.Semaphore(8)

            async def meta(mid: str) -> dict[str, Any]:
                async with sem:
                    return await self._call("GET", f"{g}/users/me/messages/{quote(mid)}", params=[("format", "metadata"), *[("metadataHeaders", h) for h in METADATA_HEADERS]])
            msgs = [mime.normalize_message(m, with_body=False) for m in await asyncio.gather(*(meta(i) for i in ids))]
            return {"data": {"messages": msgs, "next_page_token": listing.get("nextPageToken")}, "source_refs": [f"gmail:{i}" for i in ids]}
        if tool == "gmail.get_message":
            m = await self._call("GET", f"{g}/users/me/messages/{quote(a['message_id'])}", params={"format": "full"})
            return {"data": {"message": mime.normalize_message(m, with_body=True)}, "source_refs": [f"gmail:{m['id']}"]}
        if tool == "gmail.get_thread":
            t = await self._call("GET", f"{g}/users/me/threads/{quote(a['thread_id'])}", params={"format": "full"})
            msgs = [mime.normalize_message(m, with_body=True) for m in t.get("messages", [])][-20:]
            return {"data": {"thread_id": t["id"], "messages": msgs}, "source_refs": [f"gmail:{m['id']}" for m in msgs]}
        if tool == "calendar.list_calendars":
            data = await self._call("GET", f"{cal}/users/me/calendarList")
            cals = [{"id": c["id"], "summary": c.get("summary", ""), "time_zone": c.get("timeZone"), "primary": bool(c.get("primary"))} for c in data.get("items", [])]
            return {"data": {"calendars": cals}, "source_refs": []}
        if tool == "calendar.list_events":
            params = {"timeMin": a["time_min"], "timeMax": a["time_max"], "singleEvents": "true", "orderBy": "startTime", "maxResults": 250}
            if a.get("page_token"):
                params["pageToken"] = a["page_token"]
            data = await self._call("GET", f"{cal}/calendars/{quote(a['calendar_id'], safe='')}/events", params=params)
            tz = data.get("timeZone") or "UTC"
            evs = [normalize_event(e, tz) for e in data.get("items", []) if e.get("status") != "cancelled"]
            return {"data": {"events": evs, "next_page_token": data.get("nextPageToken")}, "source_refs": [f"calendar:{a['calendar_id']}/{e['id']}" for e in evs]}
        if tool == "calendar.get_event":
            e = await self.get_event(a["calendar_id"], a["event_id"])
            if e is None:
                raise ConnectorError("not_found", "event not found")
            return {"data": {"event": e}, "source_refs": [f"calendar:{a['calendar_id']}/{e['id']}"]}
        raise ConnectorError("invalid_input", f"unsupported read {tool}")

    async def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        try:
            e = await self._call("GET", f"{self.s.calendar_api_base}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}")
        except ConnectorError as err:
            if err.code == "not_found":
                return None
            raise
        return normalize_event(e, "UTC")

    async def find_sent(self, message_id_header: str) -> dict[str, Any] | None:
        q = f"rfc822msgid:{message_id_header.strip('<>')}"
        data = await self._call("GET", f"{self.s.gmail_api_base}/users/me/messages", params={"q": q, "maxResults": 5, "includeSpamTrash": "true"})
        found = data.get("messages", [])
        return {"id": found[0]["id"]} if found else None

    # ------------------------------------------------------------ writes
    async def write(self, tool: str, c: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
        g, cal = self.s.gmail_api_base, self.s.calendar_api_base
        if tool == "gmail.send_message":
            thread_id = in_reply_to = references = None
            if c.get("in_reply_to_message_id"):
                orig = await self._call("GET", f"{g}/users/me/messages/{quote(c['in_reply_to_message_id'])}",
                                        params=[("format", "metadata"), ("metadataHeaders", "Message-ID"), ("metadataHeaders", "References")])
                h = mime.headers_of(orig.get("payload", {}))
                thread_id, in_reply_to, references = orig.get("threadId"), h.get("message-id"), h.get("references")
            body: dict[str, Any] = {"raw": mime.build_raw(c, self.clock.now(), in_reply_to=in_reply_to, references=references)}
            if thread_id:
                body["threadId"] = thread_id
            sent = await self._call("POST", f"{g}/users/me/messages/send", json=body, write=True)
            return {"data": {"message_id": sent["id"], "thread_id": sent.get("threadId")}, "provider_refs": [f"gmail:{sent['id']}"]}
        if tool == "calendar.create_event":
            body = {"id": idempotency_key, "summary": c["summary"], "start": to_google_time(c["start"]), "end": to_google_time(c["end"])}
            for k in ("description", "location"):
                if c.get(k):
                    body[k] = c[k]
            e = await self._call("POST", f"{cal}/calendars/{quote(c['calendar_id'], safe='')}/events", params={"sendUpdates": "none"}, json=body, write=True)
            return {"data": {"event_id": e["id"], "etag": e.get("etag")}, "provider_refs": [f"calendar:{c['calendar_id']}/{e['id']}"]}
        if tool == "calendar.update_event":
            patch: dict[str, Any] = {}
            for k, v in c["changes"].items():
                patch[k] = to_google_time(v) if k in ("start", "end") else v
            e = await self._call("PATCH", f"{cal}/calendars/{quote(c['calendar_id'], safe='')}/events/{quote(c['event_id'], safe='')}",
                                 params={"sendUpdates": "none"}, json=patch, headers={"if-match": c["expected_etag"]}, write=True)
            return {"data": {"event_id": e["id"], "etag": e.get("etag")}, "provider_refs": [f"calendar:{c['calendar_id']}/{e['id']}"]}
        raise ConnectorError("invalid_input", f"unsupported write {tool}")


def _zone(name: str | None, fallback: str) -> str:
    try:
        ZoneInfo(name or fallback)
        return name or fallback
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def normalize_time(t: dict[str, Any], fallback_tz: str) -> dict[str, Any]:
    tz = _zone(t.get("timeZone"), fallback_tz)
    if t.get("date"):
        return {"date_time": None, "date": t["date"], "time_zone": tz}
    dt = datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00"))
    local = dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None).isoformat() if dt.tzinfo else dt.isoformat()
    return {"date_time": local, "date": None, "time_zone": tz}


def to_google_time(t: dict[str, Any]) -> dict[str, Any]:
    if t.get("date"):
        return {"date": t["date"], "timeZone": t["time_zone"]}
    return {"dateTime": t["date_time"], "timeZone": t["time_zone"]}


def normalize_event(e: dict[str, Any], fallback_tz: str) -> dict[str, Any]:
    out = {
        "id": e["id"], "etag": e.get("etag"), "summary": e.get("summary"), "description": e.get("description"),
        "location": e.get("location"), "status": e.get("status"), "updated": e.get("updated"),
        "start": normalize_time(e["start"], fallback_tz), "end": normalize_time(e["end"], fallback_tz),
    }
    for k in ("recurringEventId", "recurrence", "attendees"):
        if e.get(k):
            out[k] = e[k]
    return out


