"""Opt-in live smoke test for the Google adapter. NOT run in CI and never run automatically.

Prerequisites: a live deployment with a Google account already connected through the UI
(Connections > Connect with Google), and the broker's environment in this shell:

    OPA_MODE=live OPA_DATABASE_URL=... OPA_BROKER_KEK=... \
    OPA_GOOGLE_CLIENT_ID=... OPA_GOOGLE_CLIENT_SECRET=... \
    LIVE_SMOKE=1 LIVE_SMOKE_CONNECTION_ID=<connection id from /api/connections> \
    .venv/bin/python scripts/live_smoke_google.py

Reads only by default. Writes run only with LIVE_SMOKE_WRITES=1 and act directly through the
adapter (no approval card), so point them at throwaway targets:
    LIVE_SMOKE_CALENDAR_ID   a dedicated test calendar (never "primary")
    LIVE_SMOKE_SEND_TO       an address you own; one plain-text email is sent
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opa.broker.google.adapter import GoogleBackend
from opa.broker.ops import canonicalize_write, get_tool, new_message_id_header
from opa.broker.vault import Vault
from opa.core.clock import SystemClock
from opa.core.settings import get_settings


def need(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing {name}")
    return v


async def main() -> int:
    if os.environ.get("LIVE_SMOKE") != "1":
        sys.exit("refusing to run: set LIVE_SMOKE=1 to call real Google APIs")
    s = get_settings()
    if s.mode != "live":
        sys.exit("OPA_MODE must be live")
    conn_id = need("LIVE_SMOKE_CONNECTION_ID")
    engine = create_async_engine(s.broker_database_url or s.database_url)
    backend = GoogleBackend(s, async_sessionmaker(engine, expire_on_commit=False), Vault(need("OPA_BROKER_KEK")), conn_id, SystemClock())
    now = datetime.now(timezone.utc)

    cals = (await backend.read("calendar.list_calendars", {}))["data"]["calendars"]
    print(f"PASS calendar.list_calendars: {len(cals)} calendars")
    evs = (await backend.read("calendar.list_events", {"calendar_id": "primary", "time_min": now.isoformat(), "time_max": (now + timedelta(days=1)).isoformat(), "page_token": None}))["data"]["events"]
    print(f"PASS calendar.list_events: {len(evs)} events in the next 24 h")
    msgs = (await backend.read("gmail.search_messages", {"query": "newer_than:7d", "max_results": 5, "page_token": None}))["data"]["messages"]
    print(f"PASS gmail.search_messages: {len(msgs)} messages")
    if msgs:
        await backend.read("gmail.get_message", {"message_id": msgs[0]["id"]})
        print("PASS gmail.get_message")

    if os.environ.get("LIVE_SMOKE_WRITES") == "1":
        cal = need("LIVE_SMOKE_CALENDAR_ID")
        if cal == "primary":
            sys.exit("use a dedicated test calendar for writes, not primary")
        start = (now + timedelta(days=1)).replace(microsecond=0)
        args = {"calendar_id": cal, "summary": "OPA live smoke", "description": None, "location": None, "confirm_past": False,
                "start": {"date_time": start.isoformat(), "date": None, "time_zone": "UTC"},
                "end": {"date_time": (start + timedelta(minutes=15)).isoformat(), "date": None, "time_zone": "UTC"}}
        canonical, key = canonicalize_write(get_tool("calendar.create_event"), args, now, account="smoke")
        await backend.write("calendar.create_event", canonical, key)
        ev = await backend.get_event(cal, key)
        assert ev is not None, "created event not found by its client ID"
        print(f"PASS calendar.create_event and lookup by client ID {key}")
        upd = {"calendar_id": cal, "event_id": key, "changes": {"summary": "OPA live smoke (updated)"}, "expected_etag": ev["etag"], "before": {}}
        await backend.write("calendar.update_event", upd, None)
        print("PASS calendar.update_event with If-Match")
        if os.environ.get("LIVE_SMOKE_SEND_TO"):
            to = os.environ["LIVE_SMOKE_SEND_TO"]
            c = {"to": [to], "cc": [], "bcc": [], "subject": "OPA live smoke", "body": "Sent by scripts/live_smoke_google.py",
                 "in_reply_to_message_id": None, "from": "me", "message_id_header": new_message_id_header("opa.local")}
            await backend.write("gmail.send_message", c, None)
            assert await backend.find_sent(c["message_id_header"]), "sent message not found by Message-ID"
            print(f"PASS gmail.send_message to {to} and lookup by Message-ID")
    await engine.dispose()
    print("live smoke finished")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
