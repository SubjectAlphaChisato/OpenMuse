"""Egress proxy (design section 9.1). The worker's and browser's only route to the internet.

M2 ships the `allowlist` policy used for the worker: CONNECT to listed host:port pairs only,
connecting to the address the proxy itself resolved and checked. M6 adds the public-web policy.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
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


async def _reply(writer: asyncio.StreamWriter, status: str, body: str = "") -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())
    await writer.drain()
    writer.close()


def make_handler(policy: AllowlistPolicy):
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
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            log.warning("deny %s: %s (only CONNECT is allowed)", peer, line[:120])
            await _reply(writer, "403 Forbidden", "only CONNECT tunnels are allowed\n")
            return
        host, _, port_s = parts[1].rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            await _reply(writer, "400 Bad Request")
            return
        decision = await policy.decide(host, port)
        if not decision.allowed:
            log.warning("deny %s CONNECT %s:%s: %s", peer, host, port, decision.reason)
            await _reply(writer, "403 Forbidden", decision.reason + "\n")
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


async def serve(host: str, port: int, policy: AllowlistPolicy) -> asyncio.base_events.Server:
    return await asyncio.start_server(make_handler(policy), host, port, limit=MAX_HEADER)
