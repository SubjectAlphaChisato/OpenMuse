"""Isolated browser service (WEB-01, design section 9).

Runs Chromium through the public-web egress proxy with no other route out. It holds no provider
credentials, database access, or files. The agent can navigate, read, follow links, scroll, and use
tabs; it cannot run JavaScript, submit forms, upload, download, or keep a signed-in session.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from opa.browser.ops import UrlRejected, check_url

log = logging.getLogger("opa.browser")
MAX_TABS = 5
NAV_TIMEOUT_MS = 20_000
KEYS = {"Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", "Space"}


class BrowserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def chromium_args(proxy_server: str) -> list[str]:
    proxy_host = urlsplit(proxy_server).hostname or ""
    return [
        f"--proxy-server={proxy_server}",
        "--proxy-bypass-list=<-loopback>",  # remove Chromium's implicit localhost bypass: loopback goes through the proxy too
        f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {proxy_host}",  # Chromium never resolves names itself
        "--disable-quic",  # QUIC is UDP and would not go through an HTTP proxy
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=DnsOverHttps,MediaRouter",
        "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps",
        "--no-first-run", "--no-default-browser-check",
    ]


@dataclass
class Session:
    id: str
    owner_id: str
    task_id: str | None
    context: Any
    tabs: dict[str, Any] = field(default_factory=dict)
    links: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    current: str = "t1"
    control: str = "agent"
    stopped: bool = False
    visited: list[dict[str, str]] = field(default_factory=list)
    next_tab: int = 1
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserService:
    def __init__(self, proxy_server: str, *, idle_seconds: int = 600) -> None:
        self.proxy_server = proxy_server
        self.idle_seconds = idle_seconds
        self.sessions: dict[str, Session] = {}
        self._pw = None
        self._browser = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True, args=chromium_args(self.proxy_server))

    async def close(self) -> None:
        for s in list(self.sessions.values()):
            await self.stop(s.id)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ------------------------------------------------------------ sessions
    async def create(self, session_id: str, owner_id: str, task_id: str | None) -> dict[str, Any]:
        if session_id in self.sessions and not self.sessions[session_id].stopped:
            return self.describe(self.sessions[session_id])
        ctx = await self._browser.new_context(accept_downloads=False, service_workers="block", viewport={"width": 1280, "height": 800},
                                              java_script_enabled=True, ignore_https_errors=False)
        s = Session(session_id, owner_id, task_id, ctx)
        ctx.on("page", lambda p: asyncio.ensure_future(self._adopt_or_close(s, p)))
        s.tabs["t1"] = await ctx.new_page()
        s.next_tab = 2
        self.sessions[session_id] = s
        return self.describe(s)

    async def _adopt_or_close(self, s: Session, page: Any) -> None:
        """Pop-ups opened by page scripts (they have an opener) are closed; tabs the service opens are kept."""
        try:
            if await page.opener() is not None:
                await page.close()
        except Exception:
            pass

    def get(self, session_id: str) -> Session:
        s = self.sessions.get(session_id)
        if s is None or s.stopped:
            raise BrowserError("not_found", "this browser session has ended")
        s.last_used = time.monotonic()
        return s

    def describe(self, s: Session) -> dict[str, Any]:
        return {"session_id": s.id, "control": s.control, "stopped": s.stopped, "current_tab": s.current,
                "tabs": [{"tab_id": k, "url": p.url} for k, p in s.tabs.items()]}

    async def stop(self, session_id: str) -> dict[str, Any]:
        s = self.sessions.get(session_id)
        if s is None:
            return {"session_id": session_id, "stopped": True}
        async with s.lock:
            if not s.stopped:
                s.stopped = True
                try:
                    await s.context.close()
                except Exception:
                    pass
        return self.describe(s)

    async def set_control(self, session_id: str, control: str) -> dict[str, Any]:
        s = self.get(session_id)
        async with s.lock:
            if control == "agent" and s.control == "user":
                # Nothing the owner did while in control (e.g. signing in) carries over to the agent.
                await s.context.clear_cookies()
                for page in s.tabs.values():
                    try:
                        await page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
                    except Exception:
                        pass
            s.control = control
        return self.describe(s)

    async def reap_idle(self) -> int:
        n = 0
        for s in list(self.sessions.values()):
            if not s.stopped and time.monotonic() - s.last_used > self.idle_seconds:
                await self.stop(s.id)
                n += 1
        return n

    # ------------------------------------------------------------ agent commands
    async def command(self, session_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        s = self.get(session_id)
        async with s.lock:
            if s.stopped:
                raise BrowserError("not_found", "this browser session has ended")
            if s.control != "agent":
                raise BrowserError("paused", "the owner has taken control of the browser; wait until they resume")
            tab = args.get("tab_id") or s.current
            if action == "navigate":
                return await self._navigate(s, self._tab(s, tab), tab, args["url"])
            if action == "snapshot":
                return await self._extract(s, self._tab(s, tab), tab, blocked=None)
            if action == "click_link":
                links = s.links.get(tab, [])
                ref = int(args["ref"])
                if not 1 <= ref <= len(links):
                    raise BrowserError("invalid_input", f"there is no link {ref} in the latest result for tab {tab}")
                return await self._navigate(s, self._tab(s, tab), tab, links[ref - 1]["url"])
            if action == "scroll":
                page = self._tab(s, tab)
                await page.mouse.wheel(0, -600 if args.get("direction") == "up" else 600)
                return {"tab_id": tab, "url": page.url, "scrolled": args.get("direction", "down")}
            if action == "open_tab":
                if len(s.tabs) >= MAX_TABS:
                    raise BrowserError("invalid_input", f"at most {MAX_TABS} tabs; close one first")
                check_url_or_raise(args["url"])
                tab = f"t{s.next_tab}"
                s.next_tab += 1
                s.tabs[tab] = await s.context.new_page()
                s.current = tab
                return await self._navigate(s, s.tabs[tab], tab, args["url"])
            if action == "close_tab":
                page = self._tab(s, tab)
                if len(s.tabs) == 1:
                    raise BrowserError("invalid_input", "cannot close the last tab")
                await page.close()
                s.tabs.pop(tab)
                s.links.pop(tab, None)
                s.current = next(iter(s.tabs))
                return {"closed": tab, "current_tab": s.current}
            raise BrowserError("invalid_input", f"unknown browser action {action}")

    @staticmethod
    def _tab(s: Session, tab: str) -> Any:
        page = s.tabs.get(tab)
        if page is None:
            raise BrowserError("invalid_input", f"no tab {tab}")
        return page

    async def _navigate(self, s: Session, page: Any, tab: str, url: str) -> dict[str, Any]:
        url = check_url_or_raise(url)
        s.current = tab
        blocked = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            if resp is not None and resp.headers.get("x-opa-blocked"):
                blocked = resp.headers.get("x-opa-blocked-reason") or "blocked by the network policy"
        except Exception as e:  # Chromium reports a refused CONNECT as a tunnel failure
            text = str(e)
            if "ERR_TUNNEL_CONNECTION_FAILED" in text or "ERR_PROXY" in text or "ERR_NAME_NOT_RESOLVED" in text:
                blocked = "the destination is not a public web address"
            elif "Timeout" in text:
                raise BrowserError("provider_unavailable", "the page took too long to load") from None
            else:
                raise BrowserError("provider_unavailable", f"the page could not be opened ({text.splitlines()[0][:200]})") from None
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        s.visited.append({"url": page.url or url, "requested": url, "at": datetime.now(timezone.utc).isoformat(), "tab_id": tab,
                          "blocked": bool(blocked)})
        return await self._extract(s, page, tab, blocked=blocked, requested=url)

    async def _extract(self, s: Session, page: Any, tab: str, *, blocked: str | None, requested: str | None = None) -> dict[str, Any]:
        at = datetime.now(timezone.utc).isoformat()
        if blocked:
            s.links[tab] = []
            return {"tab_id": tab, "url": page.url, "requested": requested, "fetched_at": at, "blocked": True, "reason": blocked,
                    "title": "", "text": "", "links": []}
        try:
            title = await page.title()
            text = await page.inner_text("body", timeout=5000)
        except Exception:
            title, text = "", ""
        try:
            raw = await page.eval_on_selector_all("a[href]", "els => els.slice(0, 300).map(e => [(e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 120), e.href])")
        except Exception:
            raw = []
        links, seen = [], set()
        for label, href in raw:
            if not href.startswith(("http://", "https://")) or href in seen:
                continue
            seen.add(href)
            links.append({"ref": len(links) + 1, "text": label, "url": href})
            if len(links) >= 100:
                break
        s.links[tab] = links
        return {"tab_id": tab, "url": page.url, "requested": requested, "fetched_at": at, "blocked": False, "title": title[:300],
                "text": text, "links": links}

    # ------------------------------------------------------------ owner live view and takeover
    async def screenshot(self, session_id: str) -> bytes:
        s = self.get(session_id)
        page = s.tabs.get(s.current) or next(iter(s.tabs.values()))
        return await page.screenshot(type="jpeg", quality=60)

    async def owner_input(self, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
        s = self.get(session_id)
        async with s.lock:
            if s.control != "user":
                raise BrowserError("paused", "take control before sending input")
            page = s.tabs[s.current]
            kind = event.get("type")
            if kind == "click":
                await page.mouse.click(float(event["x"]), float(event["y"]))
            elif kind == "type":
                await page.keyboard.type(str(event.get("text", ""))[:2000])
            elif kind == "key":
                key = str(event.get("key", ""))
                if key not in KEYS:
                    raise BrowserError("invalid_input", "unsupported key")
                await page.keyboard.press(key)
            elif kind == "wheel":
                await page.mouse.wheel(0, max(-3000, min(3000, float(event.get("dy", 0)))))
            elif kind == "navigate":
                await self._navigate(s, page, s.current, str(event.get("url", "")))
            else:
                raise BrowserError("invalid_input", "unsupported input")
        return {"ok": True, "url": page.url}


def check_url_or_raise(url: str) -> str:
    try:
        return check_url(url)
    except UrlRejected as e:
        raise BrowserError("permission_denied", str(e)) from None
