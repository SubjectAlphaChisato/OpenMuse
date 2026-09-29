"""In-process fake of Google's OAuth, Gmail, and Calendar HTTP APIs for tests (no network).

It checks what the real service checks where it matters to the client: PKCE (S256), single-use
codes, exact redirect URIs, client credentials, bearer tokens with expiry on the injected clock,
per-endpoint scopes, and etag preconditions. Faults simulate lost responses and outages.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from opa.broker.google.http import hx as httpx

G = "https://www.googleapis.com/auth/"
GMAIL_READ, GMAIL_SEND, CAL_READ, CAL_WRITE = G + "gmail.readonly", G + "gmail.send", G + "calendar.readonly", G + "calendar.events"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _err(status: int, message: str, reason: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"error": {"code": status, "message": message}}
    if reason:
        body["error"]["errors"] = [{"reason": reason, "message": message}]
    return httpx.Response(status, json=body)


class FakeGoogle:
    def __init__(self, clock, *, client_id: str = "test-client", client_secret: str = "test-secret",
                 redirect_uri: str = "http://127.0.0.1:8080/api/oauth/google/callback") -> None:
        self.clock = clock
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.users = {"sub-1": "owner@gmail.example", "sub-2": "second@gmail.example"}
        self.codes: dict[str, dict[str, Any]] = {}
        self.grants: dict[str, set[str]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.access_tokens: dict[str, dict[str, Any]] = {}
        self.issued: list[str] = []  # every secret ever issued, for leak checks
        self.refresh_calls = 0
        self.revoked: list[str] = []
        self.requests: list[httpx.Request] = []
        self.faults: list[tuple[str, str]] = []  # (route key, kind)
        self._seed()
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ data
    def _seed(self) -> None:
        now = self.clock.now()
        self.messages: dict[str, dict[str, Any]] = {}
        self._add_msg("g1", "th1", "priya@partner.example", "owner@gmail.example", "Agenda for Thursday", "Bring the metrics please.", now - timedelta(days=1), ["INBOX"], mid="<a1@partner.example>")
        self._add_msg("g2", "th2", "news@shop.example", "owner@gmail.example", "Weekly deals", None, now - timedelta(hours=10), ["INBOX"],
                      html='<html><head><style>p{}</style></head><body><p>Hello <b>there</b></p><script>x()</script><p>See <a href="https://shop.example/d">deals</a></p></body></html>')
        self._add_msg("g3", "th3", "no-reply@bank.example", "owner@gmail.example", "Your verification code", "Your verification code is 771204.", now - timedelta(hours=1), ["INBOX"])
        self._add_msg("g4", "th1", "owner@gmail.example", "priya@partner.example", "Re: Agenda for Thursday", "Will do.", now - timedelta(hours=20), ["SENT"], mid="<a2@gmail.example>")
        self.next_msg = 100
        tomorrow = (now + timedelta(days=1)).date().isoformat()
        self.calendars = [{"id": "primary", "summary": "owner@gmail.example", "timeZone": "America/Los_Angeles", "primary": True, "accessRole": "owner"}]
        self.events: dict[str, dict[str, dict[str, Any]]] = {"primary": {}}
        self._add_event("primary", "gdesign1", "Design review", f"{tomorrow}T10:00:00-07:00", f"{tomorrow}T11:00:00-07:00")
        self._add_event("primary", "gweekly_1", "Weekly 1:1", f"{tomorrow}T13:00:00-07:00", f"{tomorrow}T13:30:00-07:00", recurringEventId="gweekly")
        self._add_event("primary", "gteam1", "Team sync", f"{tomorrow}T15:00:00-07:00", f"{tomorrow}T15:30:00-07:00", attendees=[{"email": "sam@team.example"}])
        ev = {"id": "gholiday", "etag": '"e-hol"', "status": "confirmed", "summary": "Holiday", "start": {"date": tomorrow}, "end": {"date": tomorrow}, "updated": now.isoformat()}
        self.events["primary"]["gholiday"] = ev
        self._bump("primary", "gholiday")

    def _add_msg(self, mid_, thread, frm, to, subject, text, when: datetime, labels, *, html: str | None = None, mid: str | None = None) -> None:
        headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
                   {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S +0000")}, {"name": "Message-ID", "value": mid or f"<{mid_}@fake>"}]
        if html is not None:
            payload = {"mimeType": "multipart/alternative", "headers": headers, "parts": [
                {"mimeType": "text/html", "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}], "body": {"data": _b64(html.encode())}}]}
            snippet = "Hello there"
        else:
            payload = {"mimeType": "text/plain", "headers": headers, "body": {"data": _b64(text.encode())}}
            snippet = text[:100].replace("'", "&#39;")
        self.messages[mid_] = {"id": mid_, "threadId": thread, "labelIds": labels, "snippet": snippet, "payload": payload, "internalDate": when.timestamp()}

    def _bump(self, cal: str, eid: str) -> None:
        self.seq = getattr(self, "seq", 0) + 1
        self.event_seq = getattr(self, "event_seq", {})
        self.event_seq[(cal, eid)] = self.seq

    def move_event(self, cal: str, eid: str, start: str, end: str) -> None:
        ev = self.events[cal][eid]
        ev["start"] = {"dateTime": start, "timeZone": "America/Los_Angeles"}
        ev["end"] = {"dateTime": end, "timeZone": "America/Los_Angeles"}
        ev["etag"] = f'"e-{secrets.token_hex(3)}"'
        self._bump(cal, eid)

    def cancel_event(self, cal: str, eid: str) -> None:
        ev = self.events[cal][eid]
        ev["status"], ev["etag"] = "cancelled", f'"e-{secrets.token_hex(3)}"'
        self._bump(cal, eid)

    def invalidate_sync_tokens(self) -> None:
        """Google may expire sync tokens at any time; older tokens then get 410."""
        self.sync_epoch = getattr(self, "sync_epoch", 0) + 1

    def _add_event(self, cal: str, eid: str, summary: str, start: str, end: str, **extra: Any) -> None:
        self.events[cal][eid] = {"id": eid, "etag": f'"e-{secrets.token_hex(3)}"', "status": "confirmed", "summary": summary,
                                 "start": {"dateTime": start, "timeZone": "America/Los_Angeles"}, "end": {"dateTime": end, "timeZone": "America/Los_Angeles"},
                                 "updated": self.clock.now().isoformat(), **extra}
        self._bump(cal, eid)

    # ------------------------------------------------------------------ test controls
    def consent(self, authorization_url: str, *, sub: str = "sub-1", grant: list[str] | None = None, deny: bool = False) -> str:
        """Simulate the owner approving the consent screen. Returns the callback path + query."""
        q = {k: v[0] for k, v in parse_qs(urlparse(authorization_url).query).items()}
        assert q["client_id"] == self.client_id and q["redirect_uri"] == self.redirect_uri
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        assert q["access_type"] == "offline" and q["prompt"] == "consent" and q["include_granted_scopes"] == "true"
        cb = urlparse(q["redirect_uri"]).path
        if deny:
            return f"{cb}?{urlencode({'error': 'access_denied', 'state': q['state']})}"
        requested = set(q["scope"].split())
        granted = requested if grant is None else requested & (set(grant) | {"openid", "email"})
        self.grants[sub] = self.grants.get(sub, set()) | granted  # include_granted_scopes
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "sub": sub}
        return f"{cb}?{urlencode({'code': code, 'state': q['state'], 'scope': ' '.join(sorted(granted))})}"

    def revoke_user(self, sub: str) -> None:
        """The owner removed the app's access from their Google account."""
        for rt, info in self.refresh_tokens.items():
            if info["sub"] == sub:
                info["revoked"] = True
        for at, info in self.access_tokens.items():
            if info["sub"] == sub:
                info["revoked"] = True

    def invalidate_access_tokens(self) -> None:
        for info in self.access_tokens.values():
            info["revoked"] = True

    def fault(self, route: str, kind: str) -> None:
        self.faults.append((route, kind))

    def sent(self) -> list[dict[str, Any]]:
        return [m for m in self.messages.values() if "SENT" in m["labelIds"] and "raw" in m]

    # ------------------------------------------------------------------ HTTP
    def _take_fault(self, route: str) -> str | None:
        for i, (r, kind) in enumerate(self.faults):
            if r == route:
                self.faults.pop(i)
                return kind
        return None

    def _issue(self, sub: str, scopes: set[str], rt: str) -> dict[str, Any]:
        at = "ya29." + secrets.token_urlsafe(24)
        self.access_tokens[at] = {"sub": sub, "scopes": scopes, "expires_at": self.clock.now() + timedelta(seconds=3599), "rt": rt, "revoked": False}
        self.issued.append(at)
        return {"access_token": at, "expires_in": 3599, "token_type": "Bearer", "scope": " ".join(sorted(scopes))}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            return self._token(request)
        if url.host == "oauth2.googleapis.com" and url.path == "/revoke":
            return self._revoke(request)
        auth = request.headers.get("authorization", "")
        tok = self.access_tokens.get(auth.removeprefix("Bearer "))
        if not tok or tok["revoked"] or tok["expires_at"] <= self.clock.now():
            return _err(401, "Request had invalid authentication credentials.", "authError")
        if url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"sub": tok["sub"], "email": self.users[tok["sub"]], "email_verified": True})
        if url.host == "gmail.googleapis.com":
            return self._gmail(request, tok)
        if url.host == "www.googleapis.com" and url.path.startswith("/calendar/v3"):
            return self._calendar(request, tok)
        return _err(404, "no such endpoint")

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            info = self.codes.pop(form.get("code", ""), None)
            if info is None or form.get("redirect_uri") != info["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"})
            if _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != info["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid code verifier."})
            scopes = self.grants[info["sub"]]
            rt = "1//" + secrets.token_urlsafe(24)
            self.refresh_tokens[rt] = {"sub": info["sub"], "scopes": scopes, "revoked": False}
            self.issued.append(rt)
            return httpx.Response(200, json={**self._issue(info["sub"], scopes, rt), "refresh_token": rt})
        if form.get("grant_type") == "refresh_token":
            self.refresh_calls += 1
            info = self.refresh_tokens.get(form.get("refresh_token", ""))
            if info is None or info["revoked"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json=self._issue(info["sub"], info["scopes"], form["refresh_token"]))
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        if self._take_fault("revoke") == "503":
            return httpx.Response(503, text="unavailable")
        token = parse_qs(request.content.decode()).get("token", [""])[0]
        info = self.refresh_tokens.get(token)
        if info is None:
            return httpx.Response(400, json={"error": "invalid_token"})
        self.revoked.append(token)
        self.revoke_user(info["sub"])
        return httpx.Response(200, json={})

    @staticmethod
    def _need(tok: dict[str, Any], *any_of: str) -> httpx.Response | None:
        if not set(any_of) & tok["scopes"]:
            return _err(403, "Request had insufficient authentication scopes.", "insufficientPermissions")
        return None

    def _gmail(self, request: httpx.Request, tok: dict[str, Any]) -> httpx.Response:
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        params = request.url.params
        if path == "/messages/send" and request.method == "POST":
            if (denied := self._need(tok, GMAIL_SEND)):
                return denied
            kind = self._take_fault("send")
            if kind == "connect_error":
                raise httpx.ConnectError("connection refused", request=request)
            body = json.loads(request.content)
            raw = base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4))
            parsed = message_from_bytes(raw, policy=policy.default)
            mid = f"g{self.next_msg}"
            self.next_msg += 1
            headers = [{"name": k, "value": str(v)} for k, v in parsed.items() if k.lower() != "bcc"]
            self.messages[mid] = {"id": mid, "threadId": body.get("threadId") or f"th{mid}", "labelIds": ["SENT"], "snippet": "",
                                  "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": _b64(parsed.get_content().encode())}},
                                  "raw": raw, "bcc": str(parsed.get("Bcc", ""))}
            if kind == "500_after":
                return _err(500, "Backend Error", "backendError")
            if kind == "timeout_after":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json={"id": mid, "threadId": self.messages[mid]["threadId"], "labelIds": ["SENT"]})
        if (denied := self._need(tok, GMAIL_READ)):
            return denied
        if path == "/messages":
            q = params.get("q", "")
            hits = sorted(self.messages.values(), key=lambda m: m.get("internalDate", 1e12), reverse=True)
            for term in q.split():
                if term.startswith("rfc822msgid:"):
                    want = term.split(":", 1)[1]
                    hits = [m for m in hits if any(h["name"].lower() == "message-id" and h["value"].strip("<>") == want for h in m["payload"]["headers"])]
                elif term == "in:sent":
                    hits = [m for m in hits if "SENT" in m["labelIds"]]
                elif term.startswith("from:"):
                    hits = [m for m in hits if any(h["name"] == "From" and term[5:] in h["value"] for h in m["payload"]["headers"])]
            size = int(params.get("maxResults", 100))
            start = int(params.get("pageToken", 0))
            page = hits[start:start + size]
            out: dict[str, Any] = {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in page], "resultSizeEstimate": len(hits)}
            if start + size < len(hits):
                out["nextPageToken"] = str(start + size)
            if not page:
                out.pop("messages")
            return httpx.Response(200, json=out)
        if path.startswith("/messages/"):
            m = self.messages.get(path.split("/")[2])
            if m is None:
                return _err(404, "Requested entity was not found.")
            view = {k: v for k, v in m.items() if k not in ("raw", "bcc", "internalDate")}
            if params.get("format") == "metadata":
                wanted = {h.lower() for h in params.get_list("metadataHeaders")}
                view["payload"] = {"mimeType": m["payload"]["mimeType"], "headers": [h for h in m["payload"]["headers"] if h["name"].lower() in wanted]}
            return httpx.Response(200, json=view)
        if path.startswith("/threads/"):
            tid = path.split("/")[2]
            msgs = [{k: v for k, v in m.items() if k not in ("raw", "bcc", "internalDate")} for m in self.messages.values() if m["threadId"] == tid]
            return httpx.Response(200, json={"id": tid, "messages": msgs}) if msgs else _err(404, "Requested entity was not found.")
        return _err(404, "no such gmail endpoint")

    def _calendar(self, request: httpx.Request, tok: dict[str, Any]) -> httpx.Response:
        path = request.url.path.removeprefix("/calendar/v3")
        params = request.url.params
        write = request.method in ("POST", "PATCH", "PUT", "DELETE")
        if (denied := self._need(tok, CAL_WRITE) if write else self._need(tok, CAL_READ, CAL_WRITE)):
            return denied
        if path == "/users/me/calendarList":
            if self._take_fault("calendarList") == "429":
                return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"code": 429, "message": "Rate Limit Exceeded"}})
            return httpx.Response(200, json={"items": self.calendars})
        parts = path.split("/")  # ['', 'calendars', cal, 'events', id?]
        if len(parts) < 4 or parts[1] != "calendars" or parts[3] != "events":
            return _err(404, "Not Found")
        cal = parts[2]
        if cal not in self.events:
            return _err(404, "Not Found")
        store = self.events[cal]
        if len(parts) == 4 and request.method == "GET":
            assert params.get("singleEvents") == "true"
            def start_of(e):
                s = e["start"]
                return datetime.fromisoformat(s["dateTime"]) if "dateTime" in s else datetime.fromisoformat(s["date"] + "T00:00:00-07:00")
            if "syncToken" in params:  # incremental sync: no time bounds allowed, deleted entries included
                assert "timeMin" not in params and "timeMax" not in params and params.get("showDeleted") == "true"
                _, epoch, since_s = params["syncToken"].split(":")
                since = int(since_s)
                if int(epoch) != getattr(self, "sync_epoch", 0):
                    return _err(410, "Sync token is no longer valid, a full sync is required.", "fullSyncRequired")
                changed = [e for (c, i), n in sorted(self.event_seq.items(), key=lambda kv: kv[1]) if c == cal and n > since for e in [store[i]]]
                items = [{"id": e["id"], "etag": e["etag"], "status": "cancelled"} if e["status"] == "cancelled" else e for e in changed]
            else:
                lo = datetime.fromisoformat(params["timeMin"])
                hi = datetime.fromisoformat(params["timeMax"]) if "timeMax" in params else None
                items = sorted([e for e in store.values() if e["status"] != "cancelled" and lo <= start_of(e) and (hi is None or start_of(e) < hi)], key=start_of)
                if hi is not None:
                    return httpx.Response(200, json={"timeZone": "America/Los_Angeles", "items": items})
            # sync requests are paged two at a time so clients must follow nextPageToken
            start = int(params.get("pageToken", 0))
            page = items[start:start + 2]
            body: dict[str, Any] = {"timeZone": "America/Los_Angeles", "items": page}
            if start + 2 < len(items):
                body["nextPageToken"] = str(start + 2)
            else:
                body["nextSyncToken"] = f"gs:{getattr(self, 'sync_epoch', 0)}:{self.seq}"
            return httpx.Response(200, json=body)
        if len(parts) == 4 and request.method == "POST":
            assert params.get("sendUpdates") == "none", "writes must not send invitations"
            body = json.loads(request.content)
            kind = self._take_fault("insert")
            if body.get("id") in store:
                return _err(409, "The requested identifier already exists.", "duplicate")
            ev = {**body, "etag": f'"e-{secrets.token_hex(3)}"', "status": "confirmed", "updated": self.clock.now().isoformat()}
            store[body["id"]] = ev
            self._bump(cal, body["id"])
            if kind == "timeout_after":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json=ev)
        eid = parts[4]
        ev = store.get(eid)
        if ev is None:
            return _err(404, "Not Found")
        if request.method == "GET":
            return httpx.Response(200, json=ev)
        if request.method == "PATCH":
            assert params.get("sendUpdates") == "none"
            if request.headers.get("if-match") != ev["etag"]:
                return _err(412, "Precondition Failed", "conditionNotMet")
            ev.update(json.loads(request.content))
            ev["etag"] = f'"e-{secrets.token_hex(3)}"'
            self._bump(cal, eid)
            return httpx.Response(200, json=ev)
        return _err(405, "method not allowed")
