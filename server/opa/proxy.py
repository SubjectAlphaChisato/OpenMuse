"""Egress proxy (design section 9.1). The worker's and browser's only route to the internet.

M2 ships the `allowlist` policy used for the worker: CONNECT to listed host:port pairs only,
connecting to the address the proxy itself resolved and checked. M6 adds the public-web policy.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit
from dataclasses import dataclass

log = logging.getLogger("opa.proxy")
MAX_HEADER = 16 * 1024


def is_public_address(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_multicast or addr.is_reserved or addr.is_loopback or addr.is_link_local or addr.is_private)


@dataclass
class Decision:
    allowed: bool
    reason: str
    address: str | None = None


class AllowlistPolicy:
    allow_plain_http = False

    def __init__(self, allow: list[str], *, require_public: bool = True) -> None:
        self.allow: set[tuple[str, int]] = set()
        for item in allow:
            host, _, port = item.rpartition(":")
            self.allow.add((host.lower(), int(port)))
        self.require_public = require_public

    async def decide(self, host: str, port: int) -> Decision:
        if (host.lower(), port) not in self.allow:
            return Decision(False, f"{host}:{port} is not on the allowlist")
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return Decision(False, f"cannot resolve {host}")
        addrs = [i[4][0] for i in infos]
        if self.require_public and not all(is_public_address(a) for a in addrs):
            return Decision(False, f"{host} resolves to a non-public address")
        return Decision(True, "allowed", addrs[0])


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _reply(writer: asyncio.StreamWriter, status: str, body: str = "", reason: str | None = None) -> None:
    extra = f"X-OPA-Blocked: 1\r\nX-OPA-Blocked-Reason: {reason}\r\n" if reason else ""
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\n{extra}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())
    await writer.drain()
    writer.close()


class PublicWebPolicy:
    """WEB-01: the browser may reach only public addresses on web ports, for every connection and redirect.

    The proxy resolves each name itself and connects to the address it checked, so DNS rebinding
    cannot swap in a private address after the check. `resolver` and `public_override` exist for
    tests and demo fixtures only (a fake DNS and names treated as public); the CLI refuses them in live mode.
    """

    allow_plain_http = True

    def __init__(self, ports: set[int] | None = None, *, resolver: dict[str, str] | None = None,
                 public_override: dict[str, int] | None = None) -> None:
        self.ports = ports or {80, 443}
        self.resolver = {k.lower(): v for k, v in (resolver or {}).items()}
        self.public_override = {k.lower(): v for k, v in (public_override or {}).items()}  # name -> the one port allowed for it
        self.denials: list[tuple[str, int, str]] = []

    def _deny(self, host: str, port: int, reason: str) -> Decision:
        self.denials.append((host, port, reason))
        return Decision(False, reason)

    async def decide(self, host: str, port: int) -> Decision:
        name = host.lower().rstrip(".")
        override_port = self.public_override.get(name)
        try:
            addrs = [str(ipaddress.ip_address(name))]
        except ValueError:
            if name in self.resolver:
                addrs = [self.resolver[name]]
            elif self.resolver:  # a test resolver is authoritative: unknown names do not exist
                return self._deny(host, port, "name does not resolve")
            else:
                try:
                    infos = await asyncio.get_running_loop().getaddrinfo(name, port, type=socket.SOCK_STREAM)
                except socket.gaierror:
                    return self._deny(host, port, "name does not resolve")
                addrs = sorted({i[4][0] for i in infos})
        if override_port is None and not all(is_public_address(a) for a in addrs):
            return self._deny(host, port, "private or reserved address")  # checked first: the more important reason
        if port != override_port if override_port is not None else port not in self.ports:
            return self._deny(host, port, "port not allowed")
        return Decision(True, "allowed", addrs[0])


def _hop_headers(head: bytes) -> tuple[str, list[tuple[str, str]], int]:
    """Parse request headers, drop hop-by-hop and proxy headers, and return (request line, headers, content length)."""
    lines = head.decode("latin-1").split("\r\n")
    request_line, headers, length = lines[0], [], 0
    drop = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "trailer", "upgrade"}
    for raw in lines[1:]:
        if not raw:
            continue
        k, _, v = raw.partition(":")
        k, v = k.strip(), v.strip()
        if k.lower() in drop:
            continue
        if k.lower() == "content-length":
            length = int(v) if v.isdigit() else 0
        headers.append((k, v))
    return request_line, headers, length


async def _forward_http(reader, writer, policy, method: str, target: str, head: bytes, peer) -> None:
    """Plain http:// request (absolute-URI form). One request per connection: the upstream is told to close,
    so a kept-alive client connection can never carry a later request to a different, unchecked host."""
    url = urlsplit(target)
    if url.scheme != "http" or not url.hostname:
        await _reply(writer, "403 Forbidden", "only http and https are allowed\n", reason="scheme not allowed")
        return
    port = url.port or 80
    decision = await policy.decide(url.hostname, port)
    if not decision.allowed:
        log.warning("deny %s %s %s: %s", peer, method, target[:200], decision.reason)
        await _reply(writer, "403 Forbidden", decision.reason + "\n", reason=decision.reason)
        return
    _, headers, length = _hop_headers(head)
    body = await reader.readexactly(length) if length else b""
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    out = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers] + ["Connection: close", "", ""]
    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(decision.address, port), timeout=15)
    except (OSError, asyncio.TimeoutError):
        await _reply(writer, "502 Bad Gateway")
        return
    up_w.write("\r\n".join(out).encode("latin-1") + body)
    await up_w.drain()
    await _pipe(up_r, writer)
    up_w.close()


def make_handler(policy):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            writer.close()
            return
        if len(head) > MAX_HEADER:
            await _reply(writer, "431 Request Header Fields Too Large")
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) != 3:
            await _reply(writer, "400 Bad Request")
            return
        method, target = parts[0].upper(), parts[1]
        if method != "CONNECT":
            if getattr(policy, "allow_plain_http", False) and "://" in target:
                await _forward_http(reader, writer, policy, method, target, head, peer)
                return
            log.warning("deny %s: %s (only CONNECT is allowed)", peer, line[:120])
            await _reply(writer, "403 Forbidden", "only CONNECT tunnels are allowed\n", reason="method not allowed")
            return
        host, _, port_s = target.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            await _reply(writer, "400 Bad Request")
            return
        decision = await policy.decide(host, port)
        if not decision.allowed:
            log.warning("deny %s CONNECT %s:%s: %s", peer, host, port, decision.reason)
            await _reply(writer, "403 Forbidden", decision.reason + "\n", reason=decision.reason)
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(decision.address, port), timeout=15)
        except (OSError, asyncio.TimeoutError):
            await _reply(writer, "502 Bad Gateway")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))

    return handle


async def serve(host: str, port: int, policy) -> asyncio.base_events.Server:
    return await asyncio.start_server(make_handler(policy), host, port, limit=MAX_HEADER)
