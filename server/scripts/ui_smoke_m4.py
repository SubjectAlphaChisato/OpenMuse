"""M4 UI smoke: Files and Memory screens against real processes and headless Chromium.

Usage: .venv/bin/python scripts/ui_smoke_m4.py [--screenshots DIR]
Requires `npm run build` in ../web first. Uses its own ports so it can run beside ui_smoke.py.
"""
from __future__ import annotations

import argparse
import io
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
EMAIL, PASSWORD, PORT, BROKER_PORT = "ui4@example.com", "ui-smoke-password-456", 8096, 8095
SCRIPT = """
match:
  - contains: "Suggest"
    steps:
      - tool_calls:
          - {name: memory__suggest, input: {text: "Prefers bullet points", tags: [style], reason: "asked for bullets twice"}}
      - text: I suggested a memory for you to review.
  - contains: "Write the brief"
    steps:
      - tool_calls:
          - {name: artifact__create_markdown, input: {name: "Agent brief", content: "# Agent brief\\n\\nMeeting at 10:00.\\n", source_refs: ["calendar:primary"]}}
      - text: Brief created.
default: {text: "OK. (scripted model)"}
"""
BOOTSTRAP = f"""
import asyncio
from opa.api.auth import hash_password
from opa.db.engine import get_sessionmaker
from opa.db.models import User
async def go():
    async with get_sessionmaker()() as db, db.begin():
        db.add(User(email={EMAIL!r}, password_hash=hash_password({PASSWORD!r})))
asyncio.run(go())
"""


def wait_http(url: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:  # connection refused or timed out while the server starts
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshots", default=None)
    args = ap.parse_args()
    if not (DIST / "index.html").exists():
        print("web/dist missing; run npm run build in web/")
        return 2
    pgdir = tempfile.mkdtemp(prefix="opa-ui4-pg-")
    srv = pgserver.get_server(pgdir)
    url = srv.get_uri().replace("postgresql://", "postgresql+asyncpg://")
    (Path(pgdir) / "script.yaml").write_text(SCRIPT)
    env = dict(os.environ, OPA_DATABASE_URL=url, OPA_BIND_PORT=str(PORT), OPA_MODE="demo", OPA_MODEL_PROVIDER="scripted",
               OPA_WEB_DIST_DIR=str(DIST), OPA_INTERNAL_TOKEN="smoke-internal-token", OPA_BROKER_URL=f"http://127.0.0.1:{BROKER_PORT}",
               OPA_BROKER_BIND_PORT=str(BROKER_PORT), OPA_SCRIPTED_MODEL_PATH=str(Path(pgdir) / "script.yaml"),
               OPA_ARTIFACT_DIR=str(Path(pgdir) / "artifacts"))
    procs: list[subprocess.Popen] = []
    try:
        subprocess.run([str(ROOT / ".venv/bin/opa"), "migrate"], env=env, check=True, capture_output=True)
        subprocess.run([str(ROOT / ".venv/bin/python"), "-c", BOOTSTRAP], env=env, check=True)
        for cmd in ("api", "broker", "worker"):
            procs.append(subprocess.Popen([str(ROOT / ".venv/bin/opa"), cmd], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_http(f"http://127.0.0.1:{PORT}/healthz")
        wait_http(f"http://127.0.0.1:{BROKER_PORT}/internal/health")
        return drive(f"http://127.0.0.1:{PORT}", args.screenshots, Path(pgdir))
    finally:
        for p in procs:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        srv.cleanup()
        shutil.rmtree(pgdir, ignore_errors=True)


def scanned_pdf() -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(72, 72, 300, 400, fill=1)
    c.showPage()
    c.save()
    return buf.getvalue()


def drive(base: str, shots: str | None, tmp: Path) -> int:
    def snap(page, name):
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"m4-{name}.png"), full_page=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1100, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        try:
            page.goto(base)
            page.get_by_label("Email").fill(EMAIL)
            page.get_by_label("Password").fill(PASSWORD)
            page.keyboard.press("Enter")
            expect(page.get_by_label("Demo mode: no real provider calls")).to_be_visible()

            # ---------------- Memory
            page.get_by_role("button", name="Memory").click()
            expect(page.get_by_role("heading", name="Memory", exact=True)).to_be_visible()
            page.get_by_label("What to remember").fill("Keep replies short.")
            page.get_by_label("Tags (comma-separated, optional; e.g. style, identity)").fill("style")
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_text("Keep replies short.").first).to_be_visible()
            expect(page.locator(".sr-only[role=status]")).to_have_text("Saved to memory.")
            print("PASS memory item added by label; live region announced it")

            page.get_by_role("button", name="Open Markdown editor").click()
            editor = page.get_by_label("Memory (Markdown)")
            editor.fill(editor.input_value() + "- Prefers morning meetings.\n")
            other = ctx.new_page()  # a concurrent change from another tab
            other.goto(base)
            other.get_by_role("button", name="Memory").click()
            other.get_by_label("What to remember").fill("Lives in Lisbon.")
            other.get_by_role("button", name="Save", exact=True).click()
            expect(other.get_by_text("Lives in Lisbon.").first).to_be_visible()
            other.close()
            md_region = page.get_by_role("region", name="Edit as Markdown")
            md_region.get_by_role("button", name="Save", exact=True).click()
            expect(md_region.get_by_role("alert")).to_contain_text("changed since you opened the editor")
            print("PASS stale Markdown save shows a 409 conflict alert and keeps the text")
            page.get_by_role("button", name="Reload latest").click()
            editor = page.get_by_label("Memory (Markdown)")
            expect(editor).to_have_value(__import__("re").compile("Lives in Lisbon"))
            editor.fill(editor.input_value() + "- Prefers morning meetings.\n")
            md_region.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_text("Prefers morning meetings.").first).to_be_visible()
            print("PASS Markdown editor saves after reload")
            page.get_by_role("button", name="Forget: Lives in Lisbon.").click()
            expect(page.get_by_text("Lives in Lisbon.")).to_have_count(0)
            print("PASS forget removes the item")
            snap(page, "memory")

            # suggestion from the assistant, accepted by keyboard
            page.get_by_role("button", name="Chat").click()
            expect(page.get_by_role("button", name="Send")).to_be_enabled()
            page.get_by_role("textbox", name="Message").fill("Suggest how I like answers")
            page.keyboard.press("Enter")
            expect(page.get_by_role("list", name="Messages").get_by_text("I suggested a memory")).to_be_visible(timeout=10_000)
            page.get_by_role("button", name="Memory").click()
            expect(page.get_by_role("heading", name="Suggestions waiting for you (1)")).to_be_visible()
            page.get_by_role("button", name="Accept: Prefers bullet points").focus()
            page.keyboard.press("Enter")
            expect(page.get_by_role("heading", name="Suggestions waiting for you (1)")).to_have_count(0)
            expect(page.get_by_text("Prefers bullet points").first).to_be_visible()
            print("PASS assistant suggestion stays pending until accepted (keyboard)")

            # ---------------- Files
            page.get_by_role("button", name="Files").click()
            expect(page.get_by_role("heading", name="Files", exact=True)).to_be_visible()
            md = tmp / "notes.md"
            md.write_text("# Notes\n\nFirst paragraph.\n\n<script>alert('x')</script>\n")
            page.get_by_label("File (TXT, MD, CSV, or PDF with text; up to 20 MiB)").set_input_files(str(md))
            page.get_by_role("button", name="Upload").click()
            expect(page.locator(".sr-only[role=status]")).to_have_text("Uploaded notes.md.")
            detail = page.get_by_role("article", name="notes.md")
            expect(detail.get_by_role("heading", name="Notes")).to_be_visible()
            expect(detail.get_by_text("<script>alert('x')</script>")).to_be_visible()  # shown as text, not executed
            print("PASS upload by label; sanitized Markdown preview")

            detail.get_by_role("button", name="Edit").click()
            box = detail.get_by_label("Edit notes.md (based on version 1)")
            box.fill(box.input_value().replace("First paragraph.", "First paragraph, revised."))
            detail.get_by_role("button", name="Save as new version").click()
            expect(page.locator(".sr-only[role=status]")).to_have_text("Saved notes.md as version 2.")
            expect(detail.get_by_label("Version")).to_have_value("2")
            expect(detail.get_by_text("First paragraph, revised.")).to_be_visible()
            detail.get_by_label("Version").select_option("1")
            expect(detail.get_by_text("First paragraph.", exact=True)).to_be_visible()
            print("PASS edit creates version 2; version 1 is kept and viewable")

            # stale edit: another writer saves first
            detail.get_by_label("Version").select_option("2")
            detail.get_by_role("button", name="Edit").click()
            csrf = page.request.get(f"{base}/api/me").json()["csrf_token"]
            art_id = page.request.get(f"{base}/api/artifacts").json()[0]["id"]
            r = page.request.patch(f"{base}/api/artifacts/{art_id}", data={"expected_version": 2, "content": "# Notes\n\nOther writer.\n"},
                                   headers={"x-csrf-token": csrf})
            assert r.status == 200, r.text()
            detail.get_by_role("button", name="Save as new version").click()
            expect(detail.get_by_role("alert")).to_contain_text("changed to a newer version")
            detail.get_by_role("button", name="Cancel").click()
            print("PASS stale file edit shows a 409 conflict alert")

            with page.expect_download() as dl:
                detail.get_by_role("link", name="Download v3").click()
            assert dl.value.suggested_filename == "notes-v3.md", dl.value.suggested_filename
            detail.get_by_role("button", name="Make PDF of v3").click()
            expect(page.get_by_role("row").filter(has_text="notes.pdf")).to_be_visible()
            print("PASS download names the version; PDF rendered as a new file")
            snap(page, "files")

            bad = tmp / "scan.pdf"
            bad.write_bytes(scanned_pdf())
            page.get_by_label("File (TXT, MD, CSV, or PDF with text; up to 20 MiB)").set_input_files(str(bad))
            page.get_by_role("button", name="Upload").click()
            expect(page.get_by_role("alert").first).to_contain_text("OCR is not supported")
            print("PASS scanned PDF gives a clear unsupported-format alert")

            # file made by the assistant is linked from chat
            page.get_by_role("button", name="Chat").click()
            expect(page.get_by_role("button", name="Send")).to_be_enabled()
            page.get_by_role("textbox", name="Message").fill("Write the brief")
            page.keyboard.press("Enter")
            msgs = page.get_by_role("list", name="Messages")
            expect(msgs.get_by_text("Brief created.")).to_be_visible(timeout=10_000)
            msgs.get_by_role("button", name="open file 1").click()
            expect(page.get_by_role("article", name="Agent_brief.md")).to_be_visible()
            expect(page.get_by_role("heading", name="Agent_brief.md")).to_be_focused()
            print("PASS chat links to the generated file; focus moves to it")
        except Exception:
            snap(page, "FAILED")
            raise
        finally:
            browser.close()
        if errors:
            print("FAIL page errors:", errors)
            return 1
    print("M4 UI smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
