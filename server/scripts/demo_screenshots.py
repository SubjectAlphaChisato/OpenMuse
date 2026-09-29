"""Demo screenshots of the chat-first app: fresh database, demo mode, scripted model, real services.

Usage: .venv/bin/python scripts/demo_screenshots.py ../docs/screenshots/demo   (needs `npm run build` in ../web)
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pgserver
from playwright.sync_api import expect, sync_playwright

SERVER = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
RUN = Path(tempfile.mkdtemp(prefix="opa-demo-"))
EMAIL, PASSWORD = "you@example.com", "demo-password-123"
API, BROKER, BROWSER, PROXY = 8190, 8191, 8192, 3190

la = ZoneInfo("America/Los_Angeles")
tomorrow = (datetime.now(la) + timedelta(days=1)).date()
t_min = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=la).isoformat()
t_max = (datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=la) + timedelta(days=1)).isoformat()

SCRIPT = f"""
match:
  - contains: "Connect my accounts"
    steps:
      - tool_calls:
          - name: connections__request
            input: {{action: connect, provider: fixture, connection_id: null, modes: null, reason: "So I can read your mail, calendar, and documents."}}
      - text: "Confirm the card and I'll get started. I start with read-only access and always ask before changing anything."
  - contains: "calendar tomorrow"
    steps:
      - tool_calls:
          - name: skill__load
            input: {{name: calendar}}
      - tool_calls:
          - name: calendar__list_events
            input: {{connection_id: "@auto", calendar_id: primary, time_min: "{t_min}", time_max: "{t_max}", page_token: null}}
      - tool_calls:
          - name: skill__load
            input: {{name: gmail}}
      - tool_calls:
          - name: gmail__search_messages
            input: {{connection_id: "@auto", query: "from:priya", max_results: 5, page_token: null}}
      - text: "Tomorrow you have four things:\\n• 10:00 Design review (1 h)\\n• 11:00 Team sync with Sam (30 min)\\n• 13:00 Weekly 1:1 (30 min)\\n• 15:00 Dentist (45 min)\\n\\nTo prepare: Priya asked you to bring the metrics to the design review. Want me to email them to her?"
  - contains: "Email Priya the metrics"
    steps:
      - tool_calls:
          - name: connections__request
            input: {{action: change, provider: fixture, connection_id: null, modes: {{gmail: read_write}}, reason: "I can only read your email right now. To send it, I need to be able to propose emails for your approval."}}
      - text: "Allow the card and tell me to go ahead. You'll still approve the exact email before it's sent."
  - contains: "Go ahead"
    steps:
      - tool_calls:
          - name: gmail__send_message
            input: {{connection_id: "@auto", to: [priya@partner.example], cc: [], bcc: [], subject: "Metrics for Thursday's design review", body: "Hi Priya,\\n\\nHere are the metrics for the design review: weekly signups 1,240 (+8%), activation 41%, and pricing page conversion 3.2%.\\n\\nSee you Thursday.", in_reply_to_message_id: m001}}
      - text: "Sent. Priya has the metrics."
default: {{text: "OK."}}
"""

srv = pgserver.get_server(str(RUN / "pg"))
url = srv.get_uri().replace("postgresql://", "postgresql+asyncpg://")
(RUN / "script.yaml").write_text(SCRIPT)
env = dict(os.environ, OPA_DATABASE_URL=url, OPA_MODE="demo", OPA_MODEL_PROVIDER="scripted", OPA_SCRIPTED_MODEL_PATH=str(RUN / "script.yaml"),
           OPA_INTERNAL_TOKEN="demo-internal-token", OPA_WEB_DIST_DIR=str(SERVER.parent / "web" / "dist"), OPA_BIND_PORT=str(API),
           OPA_BROKER_URL=f"http://127.0.0.1:{BROKER}", OPA_BROKER_BIND_PORT=str(BROKER), OPA_BROWSER_URL=f"http://127.0.0.1:{BROWSER}",
           OPA_BROWSER_BIND_PORT=str(BROWSER), OPA_WEB_PROXY_URL=f"http://127.0.0.1:{PROXY}", OPA_ARTIFACT_DIR=str(RUN / "artifacts"),
           OPA_EXPORT_DIR=str(RUN / "exports"))
opa = str(SERVER / ".venv/bin/opa")
subprocess.run([opa, "migrate"], env=env, check=True, capture_output=True)
subprocess.run([str(SERVER / ".venv/bin/python"), "-c", f"""
import asyncio
from sqlalchemy import text
from opa.api.auth import hash_password
from opa.db.engine import get_sessionmaker
from opa.db.models import Owner
async def go():
    async with get_sessionmaker()() as db, db.begin():
        await db.execute(text("TRUNCATE owners CASCADE"))
        db.add(Owner(email={EMAIL!r}, password_hash=hash_password({PASSWORD!r}), assistant_name="Muse"))
asyncio.run(go())
"""], env=env, check=True)
procs = [subprocess.Popen(c, env=env, cwd=SERVER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in (
    [opa, "api"], [opa, "broker"], [opa, "worker"], [opa, "proxy", "--policy", "public-web", "--listen", f"127.0.0.1:{PROXY}"], [opa, "browser"])]
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{API}/", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1000, "height": 780}, device_scale_factor=2)
        def shot(name, bottom=True):
            page.wait_for_timeout(500)
            if bottom:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(200)
            page.screenshot(path=str(OUT / f"{name}.png"))

        def say(text, wait_for):
            expect(page.get_by_role("button", name="Send")).to_be_enabled()
            page.get_by_role("textbox", name="Message").fill(text)
            page.get_by_role("button", name="Send").click()
            expect(page.get_by_text(wait_for, exact=False).last).to_be_visible(timeout=20_000)

        page.goto(f"http://127.0.0.1:{API}")
        page.get_by_label("Email").fill(EMAIL)
        page.get_by_label("Password").fill(PASSWORD)
        page.keyboard.press("Enter")
        expect(page.get_by_text("What can I help with?")).to_be_visible()
        shot("demo-1-welcome")

        say("Connect my accounts", "Confirm the card and I'll get started")
        shot("demo-2-connect-in-chat")
        page.get_by_role("article", name="Connect the demo account").get_by_role("button", name="Connect", exact=True).click()
        expect(page.get_by_text("Done: Connected demo@example.test.")).to_be_visible()

        say("What's on my calendar tomorrow? Anything I should prepare?", "Want me to email them to her?")
        shot("demo-3-brief")

        say("Email Priya the metrics", "Allow the card and tell me to go ahead")
        shot("demo-4-access-in-chat")
        page.get_by_role("article", name="Change access for demo@example.test").get_by_role("button", name="Allow").click()
        expect(page.get_by_text("Done: Access updated.")).to_be_visible()

        say("Go ahead", "Send email")
        expect(page.get_by_role("article", name="Send email")).to_be_visible(timeout=20_000)
        page.get_by_role("article", name="Send email").scroll_into_view_if_needed()
        shot("demo-5-approve-in-chat", bottom=False)
        page.get_by_role("article", name="Send email").get_by_role("button", name="Allow once").click()
        expect(page.get_by_text("Sent. Priya has the metrics.")).to_be_visible(timeout=20_000)
        shot("demo-6-sent")

        page.evaluate("window.scrollTo(0, 0)")
        page.get_by_role("button", name="Menu").click()
        shot("demo-7-menu", bottom=False)
        page.locator("#main-menu").get_by_role("button", name="Skills and accounts").click()
        expect(page.get_by_role("heading", name="Skills and accounts")).to_be_visible()
        shot("demo-8-skills-and-accounts", bottom=False)
        b.close()
    print("screenshots in", OUT)
finally:
    for pr in procs:
        pr.send_signal(signal.SIGINT)
    for pr in procs:
        try:
            pr.wait(5)
        except subprocess.TimeoutExpired:
            pr.kill()
    srv.cleanup()
