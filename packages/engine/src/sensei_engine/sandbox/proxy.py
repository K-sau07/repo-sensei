"""The registry proxy (8.2): the only way out for a sandbox's dependency installs.

Installs run on an internal Docker network with no route out; this proxy sits on that network
and on one with egress, and lets through HTTPS (``CONNECT host:443``) to the package registries
only (:data:`ALLOWED`). Everything else is refused: other hosts and ports, plain HTTP, and an
allowed name that resolves to a private, loopback or link-local address (DNS rebinding toward
the host or the cloud's metadata service). It sees only TLS bytes, never what is fetched.

The standard library only, so its image is Python and this file
(``sandbox/images/registry-proxy.Dockerfile``)::

    python proxy.py --port 3128
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Collection
from typing import Any, cast

ALLOWED = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
    }
)
PORT = 3128
HTTPS_PORT = 443
MAX_HEADER_BYTES = 8192
MAX_CONNECTIONS = 128  # tunnels open at once, all clients
# Connections from one address: one install can't take every tunnel. Installs download 8 at a
# time (INSTALL_CONCURRENCY); this leaves them room.
MAX_PER_CLIENT = 16
TUNNEL_SECONDS = 600.0  # the longest a tunnel stays open, busy or not
HEADER_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 15.0
IDLE_TIMEOUT_SECONDS = 120.0
CHUNK = 65536

log = logging.getLogger("registry-proxy")


def parse_connect(
    head: bytes, allowed: Collection[str], port_allowed: int = HTTPS_PORT
) -> tuple[str, int] | str:
    """The (host, port) a request may reach, or why not (its status line)."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
        method, target, _version = line.split(" ")
    except ValueError:
        return "400 Bad Request"
    if method != "CONNECT":
        return "405 Method Not Allowed"  # only tunnels: plain HTTP isn't let through
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit():
        return "400 Bad Request"
    host = host.lower().rstrip(".")
    if int(port) != port_allowed or host not in allowed:
        return "403 Forbidden"
    return host, port_allowed


def public(address: str) -> bool:
    """Whether ``address`` is on the public internet (not private, loopback, link-local...)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


async def resolve(host: str, port: int) -> str | None:
    """A public address of ``host`` (None: it has none)."""
    loop = asyncio.get_running_loop()
    try:
        found = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    for _family, _type, _proto, _name, sockaddr in found:
        address = str(sockaddr[0])
        if public(address):
            return address
    return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, TimeoutError):
        while chunk := await asyncio.wait_for(reader.read(CHUNK), IDLE_TIMEOUT_SECONDS):
            writer.write(chunk)
            await writer.drain()
    with contextlib.suppress(OSError):
        writer.close()


Resolver = Callable[[str, int], Awaitable[str | None]]


class Proxy:
    """``port`` and ``resolver`` are for tests (a local upstream); the defaults are the only
    ones used for real."""

    def __init__(
        self,
        allowed: Collection[str] = ALLOWED,
        *,
        port: int = HTTPS_PORT,
        resolver: Resolver = resolve,
    ) -> None:
        self.allowed = frozenset(allowed)
        self._port = port
        self._resolve = resolver
        self._slots = asyncio.Semaphore(MAX_CONNECTIONS)
        self._clients: dict[str, int] = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer: Any = writer.get_extra_info("peername")
        client = str(cast("tuple[Any, ...]", peer)[0]) if isinstance(peer, tuple) else "?"
        if self._clients.get(client, 0) >= MAX_PER_CLIENT:
            await self._refuse(writer, "429 Too Many Requests", f"from {client}")
            writer.close()
            return
        self._clients[client] = self._clients.get(client, 0) + 1
        try:
            await self._handle(reader, writer)
        finally:
            self._clients[client] -= 1
            if not self._clients[client]:
                del self._clients[client]
            with contextlib.suppress(OSError):
                writer.close()

    async def _refuse(self, writer: asyncio.StreamWriter, status: str, what: str) -> None:
        log.info("refused %s: %s", what, status)
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        with contextlib.suppress(OSError):
            await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_SECONDS)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError):
            return
        target = parse_connect(head, self.allowed, self._port)
        what = head.split(b"\r\n", 1)[0].decode("ascii", "replace")[:200]
        if isinstance(target, str):
            await self._refuse(writer, target, what)
            return
        host, port = target
        async with self._slots:  # only a request that checks out takes one
            await self._tunnel(reader, writer, host, port, what)

    async def _tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        what: str,
    ) -> None:
        address = await self._resolve(host, port)
        if address is None:
            await self._refuse(writer, "502 Bad Gateway", what)
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), CONNECT_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError):
            await self._refuse(writer, "502 Bad Gateway", what)
            return
        log.info("tunnel %s (%s)", host, address)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        tunnel = asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        try:
            await asyncio.wait_for(tunnel, TUNNEL_SECONDS)
        except TimeoutError:
            log.info("tunnel to %s closed after %g s", host, TUNNEL_SECONDS)
        finally:
            with contextlib.suppress(OSError):
                upstream_writer.close()


async def serve(host: str, port: int, allowed: Collection[str] = ALLOWED) -> None:
    proxy = Proxy(allowed)
    server = await asyncio.start_server(proxy.handle, host, port, limit=MAX_HEADER_BYTES)
    log.info("registry proxy on %s:%d for %s", host, port, ", ".join(sorted(proxy.allowed)))
    async with server:
        await server.serve_forever()


def main() -> None:  # pragma: no cover - the container's entry point
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - inside its own container
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(serve(args.host, args.port))


if __name__ == "__main__":  # pragma: no cover
    main()
