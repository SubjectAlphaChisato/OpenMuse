"""A12 and WEB-01 at the browser service: real headless Chromium through the public-web proxy."""
import asyncio

import pytest

from opa.browser.service import BrowserError
from tests.browser_fixtures import browser_service, private_server, site, web_proxy  # noqa: F401  (fixtures)


async def test_public_page_loads_with_text_and_links_and_nothing_private_is_reached(browser_service, site, private_server, web_proxy):  # noqa: F811
    policy, _ = web_proxy
    await browser_service.create("s1", "owner", "task")
    r = await browser_service.command("s1", "navigate", {"url": f"http://site.test:{site.port}/"})
    assert r["blocked"] is False and r["title"] == "OPA fixture site" and "Wren protocol was published" in r["text"]
    assert [link["text"] for link in r["links"]][:2] == ["Next page", "Tricky link"]
    await asyncio.sleep(1.0)  # let subresources, fetches, and the websocket attempt run
    assert private_server.hits == []  # A12: no subresource, fetch, frame, or websocket reached a private address
    denied = {(h, reason) for h, _, reason in policy.denials}
    assert ("10.0.0.1", "private or reserved address") in denied
    assert any(h == "169.254.169.254" for h, _, _ in policy.denials)
    assert any(h == "private.test" and reason == "private or reserved address" for h, _, reason in policy.denials)


async def test_redirect_to_private_address_is_blocked(browser_service, site, private_server, web_proxy):  # noqa: F811
    policy, _ = web_proxy
    await browser_service.create("s1", "owner", "task")
    first = await browser_service.command("s1", "navigate", {"url": f"http://site.test:{site.port}/"})
    tricky = next(link["ref"] for link in first["links"] if link["text"] == "Tricky link")
    r = await browser_service.command("s1", "click_link", {"ref": tricky})
    assert r["blocked"] is True and "private" in r["reason"]
    assert private_server.hits == []
    assert any(h == "127.0.0.1" and p == private_server.port for h, p, _ in policy.denials)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/", "http://[::1]:8080/", "http://169.254.169.254/latest/",
                                 "http://10.1.2.3/", "javascript:alert(1)", "http://user:pw@site.test/", "ftp://site.test/"])
async def test_disallowed_addresses_are_refused_before_the_browser(browser_service, url):  # noqa: F811
    await browser_service.create("s1", "owner", "task")
    with pytest.raises(BrowserError) as e:
        await browser_service.command("s1", "navigate", {"url": url})
    assert e.value.code == "permission_denied"


@pytest.mark.parametrize("host", ["private.test", "rebind.test", "0x7f000001", "2130706433"])
async def test_names_and_encodings_that_resolve_privately_are_blocked_by_the_proxy(browser_service, private_server, host):  # noqa: F811
    await browser_service.create("s1", "owner", "task")
    r = await browser_service.command("s1", "navigate", {"url": f"http://{host}:{private_server.port}/admin"})
    assert r["blocked"] is True
    assert private_server.hits == []


async def test_takeover_pauses_agent_and_resume_clears_owner_state(browser_service, site):  # noqa: F811
    await browser_service.create("s1", "owner", "task")
    await browser_service.command("s1", "navigate", {"url": f"http://site.test:{site.port}/"})
    s = browser_service.sessions["s1"]
    assert any(c["name"] == "tracker" for c in await s.context.cookies())
    await browser_service.set_control("s1", "user")
    with pytest.raises(BrowserError) as e:  # A13: agent commands are paused
        await browser_service.command("s1", "snapshot", {})
    assert e.value.code == "paused"
    await browser_service.owner_input("s1", {"type": "wheel", "dy": 200})  # the owner can drive
    await browser_service.set_control("s1", "agent")
    assert await s.context.cookies() == []  # nothing from the owner's turn carries over
    with pytest.raises(BrowserError):
        await browser_service.owner_input("s1", {"type": "type", "text": "x"})  # owner input needs control
    r = await browser_service.command("s1", "snapshot", {})
    assert r["title"] == "OPA fixture site"


async def test_tabs_scroll_screenshot_and_stop(browser_service, site):  # noqa: F811
    await browser_service.create("s1", "owner", "task")
    await browser_service.command("s1", "navigate", {"url": f"http://site.test:{site.port}/"})
    t2 = await browser_service.command("s1", "open_tab", {"url": f"http://site.test:{site.port}/page2"})
    assert t2["tab_id"] == "t2" and t2["title"] == "Second page"
    assert (await browser_service.command("s1", "scroll", {"direction": "down"}))["scrolled"] == "down"
    shot = await browser_service.screenshot("s1")
    assert shot[:3] == b"\xff\xd8\xff"  # JPEG
    await browser_service.command("s1", "close_tab", {"tab_id": "t2"})
    await browser_service.stop("s1")
    with pytest.raises(BrowserError) as e:
        await browser_service.command("s1", "snapshot", {})
    assert e.value.code == "not_found"
