"""End-to-end UI smoke: real Postgres, API process serving web/dist, worker process, headless Chromium.

Usage: .venv/bin/python scripts/ui_smoke.py [--screenshots DIR]
Requires `npm run build` in ../web first.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pgserver
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT.parent / "web" / "dist"
EMAIL, PASSWORD, PORT, BROKER_PORT, BROWSER_PORT, WEB_PROXY_PORT = "ui@example.com", "ui-smoke-password-123", 8098, 8097, 8096, 3139
SCRIPT = """
match:
  - contains: "Email Priya"
    steps:
      - tool_calls:
          - name: gmail__send_message
            input: {connection_id: "@auto", to: [priya@partner.example], cc: [], bcc: [], subject: Metrics for Thursday, body: "Hi Priya, here are the metrics.", in_reply_to_message_id: null}
      - text: Sent the email to Priya.
  - contains: "Research Wren"
    steps:
      - tool_calls:
          - name: browser__navigate
            input: {url: "@site", tab_id: null}
      - tool_calls:
          - name: ask_user
            input: {question: "I found the protocol page. Keep going?"}
      - text: "The Wren protocol was published in 2024."
  - contains: "Let the assistant send email"
    steps:
      - tool_calls:
          - name: connections__request
            input: {action: change, provider: fixture, connection_id: null, modes: {gmail: read_write}, reason: "You asked me to send email for you."}
      - text: Confirm the card and I can propose emails for you to approve.
  - contains: "Connect the demo account"
    steps:
      - tool_calls:
          - name: connections__request
            input: {action: connect, provider: fixture, connection_id: null, modes: null, reason: "You asked me to connect it."}
      - text: Confirm the card to connect the demo account.
default: {text: "OK. (scripted model)"}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshots", default=None)
    args = ap.parse_args()
    if not (DIST / "index.html").exists():
        print("web/dist missing; run npm run build in web/")
        return 2

    pgdir = tempfile.mkdtemp(prefix="opa-ui-pg-")
    srv = pgserver.get_server(pgdir)
    url = srv.get_uri().replace("postgresql://", "postgresql+asyncpg://")
    script_path = Path(pgdir) / "script.yaml"
    script_path.write_text(SCRIPT)
    env = dict(os.environ, OPA_DATABASE_URL=url, OPA_BIND_PORT=str(PORT), OPA_MODE="demo", OPA_MODEL_PROVIDER="scripted",
               OPA_WEB_DIST_DIR=str(DIST), OPA_INTERNAL_TOKEN="smoke-internal-token", OPA_BROKER_URL=f"http://127.0.0.1:{BROKER_PORT}",
               OPA_BROKER_BIND_PORT=str(BROKER_PORT), OPA_SCRIPTED_MODEL_PATH=str(script_path),
               OPA_BROWSER_URL=f"http://127.0.0.1:{BROWSER_PORT}", OPA_BROWSER_BIND_PORT=str(BROWSER_PORT),
               OPA_WEB_PROXY_URL=f"http://127.0.0.1:{WEB_PROXY_PORT}", OPA_EXPORT_DIR=str(Path(pgdir) / "exports"),
               OPA_ARTIFACT_DIR=str(Path(pgdir) / "artifacts"))
    site_port = _start_site()
    script_path.write_text(SCRIPT.replace("@site", f"http://site.test:{site_port}/"))
    procs: list[subprocess.Popen] = []
    try:
        subprocess.run([str(ROOT / ".venv/bin/opa"), "migrate"], env=env, check=True, capture_output=True)
        subprocess.run([str(ROOT / ".venv/bin/python"), "-c", _BOOTSTRAP], env=env, check=True)
        procs.append(subprocess.Popen([str(ROOT / ".venv/bin/opa"), "api"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen([str(ROOT / ".venv/bin/opa"), "broker"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen([str(ROOT / ".venv/bin/opa"), "proxy", "--policy", "public-web", "--listen", f"127.0.0.1:{WEB_PROXY_PORT}",
                                       "--fixture-host", f"site.test=127.0.0.1:{site_port}"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen([str(ROOT / ".venv/bin/opa"), "browser"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen([str(ROOT / ".venv/bin/opa"), "worker"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        _wait_http(f"http://127.0.0.1:{PORT}/healthz")
        _wait_http(f"http://127.0.0.1:{BROKER_PORT}/internal/health")
        _wait_http(f"http://127.0.0.1:{BROWSER_PORT}/internal/health", timeout=40)
        return _drive(f"http://127.0.0.1:{PORT}", args.screenshots or tempfile.mkdtemp(prefix="opa-ui-fail-"))
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        srv.cleanup()
        shutil.rmtree(pgdir, ignore_errors=True)


_BOOTSTRAP = f"""
import asyncio
from opa.api.auth import hash_password
from opa.db.engine import get_sessionmaker
from opa.db.models import Owner
async def go():
    async with get_sessionmaker()() as db, db.begin():
        db.add(Owner(email={EMAIL!r}, password_hash=hash_password({PASSWORD!r})))
asyncio.run(go())
"""


def go(page, name: str) -> None:
    """Chat is the app; other screens are one menu away."""
    if name == "Chat":
        chat = page.get_by_role("navigation", name="Main").get_by_role("button", name="Chat", exact=True)
        if chat.count():
            chat.click()
        return
    page.get_by_role("button", name="Menu").click()
    page.locator("#main-menu").get_by_role("button", name=name, exact=True).click()


def _wait_http(url: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def _drive(base: str, shots: str | None) -> int:
    def snap(page, name):
        _axe(page, name)
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"{name}.png"), full_page=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: print(f"  api {r.request.method} {r.url.split(str(PORT))[-1]} -> {r.status}") if "/api/" in r.url and (r.status >= 400 or os.environ.get("SMOKE_VERBOSE")) and not r.url.endswith("/api/me") else None)
        page.on("console", lambda m: print("  console:", m.text) if m.type == "error" else None)

        page.goto(base)
        expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
        _axe(page, "0-sign-in")
        # keyboard-only sign-in
        page.get_by_label("Email").focus()
        page.keyboard.type(EMAIL)
        page.keyboard.press("Tab")
        page.keyboard.type(PASSWORD)
        page.keyboard.press("Enter")
        expect(page.get_by_label("Demo mode: no real provider calls")).to_be_visible()
        print("PASS sign-in by keyboard; DEMO badge visible")
        snap(page, "1-chat-empty")

        page.get_by_role("textbox", name="Message").fill("Summarize my week")
        page.keyboard.press("Enter")
        messages = page.get_by_role("list", name="Messages")
        expect(messages.get_by_text("Summarize my week")).to_be_visible()
        expect(messages.get_by_text("OK. (scripted model)")).to_be_visible(timeout=10_000)
        print("PASS message sent, worker reply rendered live")
        snap(page, "2-chat-reply")

        go(page, "Tasks")
        expect(page.get_by_role("heading", name="Tasks")).to_be_visible()
        row = page.get_by_role("row").filter(has_text="Summarize my week")
        expect(row.get_by_text("Task: Done")).to_be_visible()
        row.get_by_role("button", name="Retry").click()
        expect(page.get_by_role("row").filter(has_text="(retry)")).to_have_count(1, timeout=10_000)
        print("PASS tasks screen lists task; Retry creates linked task")
        snap(page, "3-tasks")

        go(page, "Notifications")
        expect(page.get_by_text("Task completed").first).to_be_visible()
        page.get_by_role("button", name="Mark read").first.click()
        print("PASS notification inbox shows completion; mark-read works")
        snap(page, "4-notifications")

        try:
            _m2(page, snap)
            _m5(page, snap)
            _m6(page, snap)
        except Exception:
            snap(page, "FAILED")
            print("FAIL screenshot saved to", shots)
            raise
        go(page, "Sign out")
        expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
        print("PASS sign-out returns to login")
        browser.close()

        if errors:
            print("FAIL page errors:", errors)
            return 1
    if AXE_FINDINGS:
        print(f"FAIL {len(AXE_FINDINGS)} serious or critical accessibility violation(s)")
        return 1
    print("PASS axe-core found no serious or critical violations on any screen checked")
    print("UI smoke passed")
    return 0


def _m2(page, snap) -> None:
        # ---- v0.2 harness: the assistant asks to connect in chat; only the owner's click connects
        go(page, "Chat")
        expect(page.get_by_role("button", name="Send")).to_be_enabled()
        page.get_by_role("textbox", name="Message").fill("Connect the demo account")
        page.keyboard.press("Enter")
        card = page.get_by_role("article", name="Connect the demo account")
        expect(card.get_by_role("button", name="Connect", exact=True)).to_be_visible(timeout=10_000)
        expect(card.get_by_text("You asked me to connect it.", exact=False)).to_be_visible()
        snap(page, "5a-chat-connect-card")
        card.get_by_role("button", name="Connect", exact=True).click()
        expect(card.get_by_text("Done: Connected demo@example.test.")).to_be_visible()
        print("PASS the assistant asks in chat; the owner's confirmation connects the demo account")
        go(page, "Skills and accounts")
        gmail = page.get_by_role("article", name="Gmail skill")
        expect(gmail.get_by_text("Ready")).to_be_visible()
        expect(page.get_by_role("article", name="Web browser skill")).to_be_visible()
        snap(page, "5b-skills")
        print("PASS skills screen shows each skill's status")

        # ---- M2: allow Gmail changes from the chat, then approve an email inside the chat by keyboard
        go(page, "Chat")
        expect(page.get_by_role("button", name="Send")).to_be_enabled()
        page.get_by_role("textbox", name="Message").fill("Let the assistant send email")
        page.keyboard.press("Enter")
        change = page.get_by_role("article", name="Change access for demo@example.test")
        expect(change.get_by_text("Gmail: Read only → Read and propose changes")).to_be_visible(timeout=10_000)
        snap(page, "5-chat-change-card")
        change.get_by_role("button", name="Allow").click()
        expect(change.get_by_text("Done: Access updated.")).to_be_visible()
        print("PASS access widened only by the owner's click on the chat card")
        expect(page.get_by_role("button", name="Send")).to_be_enabled()
        page.get_by_role("textbox", name="Message").fill("Email Priya the metrics")
        page.keyboard.press("Enter")
        expect(page.get_by_role("button", name="Approvals, 1 waiting")).to_be_visible(timeout=10_000)
        expect(page.get_by_text("Task: Needs approval")).to_be_visible()
        print("PASS proposal pauses the task and raises an approval count")

        card = page.get_by_role("article", name="Send email")
        expect(card.get_by_text("priya@partner.example").first).to_be_visible()
        expect(card.get_by_text("Hi Priya, here are the metrics.")).to_be_visible()
        expect(card.get_by_text("Sent email cannot be recalled", exact=False)).to_be_visible()
        snap(page, "6-approval-card")
        card.get_by_role("button", name="Deny").focus()
        page.keyboard.press("Shift+Tab")  # keyboard-only: move to Allow once and press Enter
        expect(card.get_by_role("button", name="Allow once")).to_be_focused()
        page.keyboard.press("Enter")
        expect(card).to_have_count(0, timeout=10_000)
        print("PASS approval card in the chat shows exact recipients and body; allowed with keyboard only")

        expect(page.get_by_role("list", name="Messages").get_by_text("Sent the email to Priya.")).to_be_visible(timeout=10_000)
        print("PASS worker dispatched through the broker after approval; task completed")
        snap(page, "7-chat-after-send")



def _m5(page, snap) -> None:
    # ---- M5: create a weekday schedule through preview and confirmation, turn it off, delete it
    page.on("dialog", lambda d: d.accept())
    go(page, "Automations")
    page.get_by_role("button", name="New automation").click()
    page.get_by_label("Name").fill("Evening review")
    page.get_by_label("What should happen each time?").fill("Summarize what happened today")
    page.get_by_label("Time (24-hour)").fill("18:00")
    page.get_by_role("button", name="Preview schedule").click()
    preview = page.get_by_role("status").filter(has_text="Every weekday at 18:00")
    expect(preview).to_be_visible()
    expect(preview.get_by_role("listitem")).to_have_count(3)
    snap(page, "8-automation-preview")
    page.get_by_role("button", name="Confirm and activate").click()
    card = page.get_by_role("article", name="Evening review")
    expect(card.get_by_text("Active")).to_be_visible()
    expect(card.get_by_text("Every weekday at 18:00")).to_be_visible()
    print("PASS automation previewed with next runs, then confirmed and active")
    snap(page, "9-automations")
    card.get_by_role("button", name="Turn off").click()
    expect(card.get_by_text("Off", exact=True)).to_be_visible()
    card.get_by_role("button", name="Delete").click()
    expect(page.get_by_text("No automations yet.")).to_be_visible()
    print("PASS automation turned off and deleted")


def _start_site() -> int:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"<html><head><title>Wren protocol</title></head><body><h1>The Wren protocol</h1><p>Published in 2024.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def _m6(page, snap) -> None:
    # ---- A17a: the M4 screens get the same axe-core scan
    for screen in ("Files", "Memory"):
        go(page, screen)
        expect(page.get_by_role("heading", name=screen if screen != "Files" else "Files", level=2).first).to_be_visible()
        _axe(page, f"m4-{screen.lower()}")
    # ---- M6: the agent browses; the owner watches live, takes control, hands back, stops
    go(page, "Chat")
    expect(page.get_by_role("button", name="Send")).to_be_enabled()
    page.get_by_role("textbox", name="Message").fill("Research Wren")
    page.keyboard.press("Enter")
    expect(page.get_by_text("I found the protocol page. Keep going?")).to_be_visible(timeout=30_000)
    go(page, "Browser")
    card = page.get_by_role("article", name="Browser for Research Wren")
    expect(card).to_be_visible()
    card.get_by_role("button", name="Show live view").click()
    live = card.get_by_role("img", name="Live view of the assistant's browser", exact=False)
    expect(live).to_be_visible(timeout=15_000)
    card.get_by_text("Pages visited (1)").click()
    expect(card.get_by_text("Wren protocol —", exact=False)).to_be_visible()
    print("PASS agent browsed a public page; live view streams frames; visited pages listed")
    snap(page, "10-browser-live")
    card.get_by_role("button", name="Take control").click()
    expect(card.get_by_text("You are in control. The assistant is paused.")).to_be_visible()
    card.get_by_role("button", name="Scroll down").click()
    card.get_by_role("button", name="Resume").click()
    expect(card.get_by_text("The assistant is in control.")).to_be_visible()
    print("PASS take control and explicit resume from the UI")
    card.get_by_role("button", name="Stop").click()
    expect(page.get_by_text("No browser is open.")).to_be_visible()
    print("PASS stop closes the browser")

    # ---- M6: export, then delete all data behind a typed confirmation
    go(page, "Your data")
    page.get_by_role("button", name="Create export").click()
    link = page.get_by_role("link", name="opa-export-", exact=False).first
    expect(link).to_be_visible()
    with page.expect_download() as dl:
        link.click()
    assert dl.value.suggested_filename.startswith("opa-export-")
    print("PASS export created and downloaded")
    delete = page.get_by_role("button", name="Delete all data")
    expect(delete).to_be_disabled()
    page.get_by_label("Type your email address", exact=False).fill(EMAIL)
    expect(delete).to_be_enabled()
    delete.click()
    expect(page.get_by_role("status").filter(has_text="Backups made before now")).to_be_visible(timeout=15_000)
    snap(page, "11-data-deleted")
    print("PASS delete-all needs the typed email and reports what happened")


AXE = ROOT.parent / "web" / "node_modules" / "axe-core" / "axe.min.js"
AXE_FINDINGS: list[str] = []


def _axe(page, label: str) -> None:
    """A17a: run axe-core on the current screen; serious and critical violations fail the smoke."""
    if not AXE.exists():
        raise RuntimeError("axe-core is missing; run npm install in web/")
    page.add_script_tag(path=str(AXE))
    result = page.evaluate("""async () => {
        const r = await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'] } });
        return r.violations.map(v => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 3).map(n => n.target.join(' ')) }));
    }""")
    for v in result:
        line = f"{label}: {v['impact']} {v['id']} - {v['help']} at {v['nodes']}"
        if v["impact"] in ("serious", "critical"):
            AXE_FINDINGS.append(line)
        print(("  axe FAIL " if v["impact"] in ("serious", "critical") else "  axe note ") + line)


if __name__ == "__main__":
    sys.exit(main())
