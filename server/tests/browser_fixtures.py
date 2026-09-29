"""A public fixture site, a 'private' server that must never be reached, and the public-web proxy."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from opa.browser.service import BrowserService
from opa.proxy import PublicWebPolicy, serve


class _Server:
    def __init__(self, handler_factory):
        self.hits: list[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def _site_handler(site: "_Server"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            site.hits.append(self.path)
            priv = site.private_port
            if self.path.startswith("/redirect-private"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{priv}/admin")
                self.end_headers()
                return
            if self.path.startswith("/page2"):
                body = "<html><head><title>Second page</title></head><body><h1>Findings</h1><p>The Wren protocol has three phases.</p></body></html>"
            elif self.path.startswith("/search"):
                body = f"<html><head><title>Search</title></head><body><p>Results for {self.path}</p></body></html>"
            else:
                body = f"""<html><head><title>OPA fixture site</title><script src="http://10.0.0.1/x.js"></script></head><body>
<h1>Public research page</h1><p>The Wren protocol was published in 2024.</p>
<a href="/page2">Next page</a> <a href="/redirect-private">Tricky link</a> <a href="http://private.test:{priv}/admin">Private admin</a>
<img src="http://127.0.0.1:{priv}/pixel.png"><img src="http://169.254.169.254/latest/meta-data/"><iframe src="http://private.test:{priv}/frame"></iframe>
<script>
fetch("http://127.0.0.1:{priv}/api").catch(() => {{}});
fetch("http://rebind.test:{priv}/x").catch(() => {{}});
try {{ new WebSocket("ws://127.0.0.1:{priv}/ws"); }} catch (e) {{}}
document.cookie = "tracker=1; path=/";
</script></body></html>"""
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H


def _private_handler(srv: "_Server"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            srv.hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"internal secret")
    return H


@pytest.fixture
def private_server():
    s = _Server(_private_handler)
    yield s
    s.close()


@pytest.fixture
def site(private_server):
    s = _Server(_site_handler)
    s.private_port = private_server.port
    yield s
    s.close()


@pytest_asyncio.fixture
async def web_proxy(site):
    # A fake DNS: site.test is the public fixture; private.test and rebind.test resolve to private addresses.
    policy = PublicWebPolicy(resolver={"site.test": "127.0.0.1", "private.test": "127.0.0.1", "rebind.test": "10.0.0.7"},
                             public_override={"site.test": site.port})
    server = await serve("127.0.0.1", 0, policy)
    yield policy, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    server.close()


@pytest_asyncio.fixture
async def browser_service(web_proxy):
    svc = BrowserService(web_proxy[1])
    await svc.start()
    yield svc
    await svc.close()
