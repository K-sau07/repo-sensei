"""OAuth clients (7.2): who may ask for tokens.

Three kinds, as the MCP authorization spec (2026-07-28) has them:

* **Metadata documents** (preferred): the ``client_id`` is an https URL whose JSON describes the
  client (``client_id`` equal to that URL, ``client_name``, ``redirect_uris``). It is fetched
  when first seen and again once its cache time is up; fetching guards against being turned on
  the server's own network (no private addresses, connecting to the address checked, no
  redirects, a size and time limit), and a document that can't be had isn't fetched again for
  a minute.
* **Dynamic registration** (RFC 7591; deprecated by MCP but what editors still use): a client
  posts its metadata and is given a ``client_id``.
* **The CLI**, known in advance as :data:`CLI_CLIENT_ID` (the device flow; no redirects).

Every client is public (no secret: ``token_endpoint_auth_method`` "none"); PKCE protects the
code. A redirect URI is https, or http on the loopback interface (a native app's).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import secrets
import socket
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import ClientKind, OAuthClient

CLI_CLIENT_ID = "sensei-cli"
CLI_CLIENT_NAME = "repo-sensei CLI"
GRANT_CODE = "authorization_code"
GRANT_REFRESH = "refresh_token"
GRANT_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"
GRANT_TYPES = (GRANT_CODE, GRANT_REFRESH, GRANT_DEVICE)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
MAX_REDIRECTS = 10
MAX_DOCUMENT_BYTES = 16 * 1024
FETCH_TIMEOUT = 5.0
MIN_FRESH = timedelta(minutes=1)
MAX_FRESH = timedelta(hours=24)
DEFAULT_FRESH = timedelta(hours=1)
_MAX_AGE = re.compile(r"max-age=(\d+)")
FAILURE_TTL = timedelta(minutes=1)
MAX_FAILURES_KEPT = 1_000


class ClientError(Exception):
    """A client that can't be used: ``error`` is the OAuth error code to answer with."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def is_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def valid_redirect(uri: str) -> bool:
    """https, or http on the loopback interface; absolute, without a fragment."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.netloc or parts.username or parts.password:
        return False
    return parts.scheme == "https" or is_loopback(uri)


def redirect_allowed(client: OAuthClient, uri: str) -> bool:
    """``uri`` is one of the client's, exactly; for a loopback one, on any port (RFC 8252
    §7.3: a native app picks a free port when it starts)."""
    if uri in client.redirect_uris:
        return True
    if not is_loopback(uri):
        return False
    asked = urlsplit(uri)
    for registered in client.redirect_uris:
        known = urlsplit(registered)
        if (
            is_loopback(registered)
            and known.hostname == asked.hostname
            and known.path == asked.path
            and known.query == asked.query
        ):
            return True
    return False


def _redirects(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_REDIRECTS:  # pyright: ignore[reportUnknownArgumentType]
        raise ClientError("invalid_redirect_uri", "Give up to 10 redirect URIs.")
    uris = [str(v) for v in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if not all(isinstance(v, str) and valid_redirect(v) for v in value):  # pyright: ignore[reportUnknownVariableType]
        raise ClientError("invalid_redirect_uri", "A redirect URI is https, or http on localhost.")
    return uris


def _public_only(meta: Mapping[str, Any]) -> None:
    method = meta.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise ClientError(
            "invalid_client_metadata",
            "Only public clients (token_endpoint_auth_method 'none', with PKCE) are supported.",
        )


async def register(db: AsyncSession, meta: Mapping[str, Any]) -> OAuthClient:
    """Dynamic registration (RFC 7591): a new public client."""
    grants = meta.get("grant_types", [GRANT_CODE, GRANT_REFRESH])
    if not isinstance(grants, list) or not grants or any(g not in GRANT_TYPES for g in grants):  # pyright: ignore[reportUnknownVariableType]
        raise ClientError("invalid_client_metadata", "Unsupported grant_types.")
    redirects = _redirects(meta.get("redirect_uris", []))
    if GRANT_CODE in grants and not redirects:
        raise ClientError("invalid_redirect_uri", "Give at least one redirect URI.")
    _public_only(meta)
    name = str(meta.get("client_name") or "MCP client").strip()[:200] or "MCP client"
    client = OAuthClient(
        client_id="rsc_" + secrets.token_urlsafe(18),
        kind=ClientKind.DYNAMIC,
        client_name=name,
        client_uri=_https_or_none(meta.get("client_uri")),
        redirect_uris=redirects,
        meta={"grant_types": list(grants)},  # pyright: ignore[reportUnknownArgumentType]
    )
    db.add(client)
    await db.flush()
    return client


def _https_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") else None


def grant_types(client: OAuthClient) -> list[str]:
    if client.client_id == CLI_CLIENT_ID:
        return [GRANT_DEVICE, GRANT_REFRESH]
    found = client.meta.get("grant_types")
    if isinstance(found, list):
        return [str(g) for g in found]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return [GRANT_CODE, GRANT_REFRESH]


class ClientRegistry:
    """Finds a client by its ``client_id``, fetching metadata documents as needed."""

    def __init__(
        self, http: Callable[[], httpx.AsyncClient | None], *, allow_private: bool = False
    ) -> None:
        self._http = http  # the app's client, once it has one
        self._allow_private = allow_private
        # Documents that couldn't be had, not asked for again for a while (anyone can name any
        # URL as a client_id, signed in or not).
        self._failed: dict[str, tuple[datetime, ClientError]] = {}

    def _remember_failure(self, url: str, error: ClientError, now: datetime) -> None:
        if len(self._failed) >= MAX_FAILURES_KEPT:
            self._failed = {u: f for u, f in self._failed.items() if f[0] > now}
            if len(self._failed) >= MAX_FAILURES_KEPT:
                self._failed.pop(next(iter(self._failed)))
        self._failed[url] = (now + FAILURE_TTL, error)

    async def get(self, db: AsyncSession, client_id: str, *, now: datetime) -> OAuthClient:
        if client_id == CLI_CLIENT_ID:
            return await self._cli(db)
        found = await db.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id))
        if client_id.startswith("https://"):
            if found is None or found.fresh_until is None or found.fresh_until <= now:
                failed = self._failed.get(client_id)
                if failed is not None and failed[0] > now:
                    raise failed[1]  # tried a moment ago: not fetched again yet
                try:
                    return await self._fetch(db, client_id, found, now=now)
                except ClientError as error:
                    self._remember_failure(client_id, error, now)
                    raise
            return found
        if found is None:
            raise ClientError("invalid_client", "Unknown client.")
        return found

    async def _cli(self, db: AsyncSession) -> OAuthClient:
        found = await db.scalar(select(OAuthClient).where(OAuthClient.client_id == CLI_CLIENT_ID))
        if found is not None:
            return found
        client = OAuthClient(
            client_id=CLI_CLIENT_ID,
            kind=ClientKind.DYNAMIC,
            client_name=CLI_CLIENT_NAME,
            redirect_uris=[],
            meta={"grant_types": [GRANT_DEVICE, GRANT_REFRESH]},
        )
        db.add(client)
        await db.flush()
        return client

    async def _fetch(
        self, db: AsyncSession, url: str, found: OAuthClient | None, *, now: datetime
    ) -> OAuthClient:
        meta, fresh = await self.read_document(url)
        redirects = _redirects(meta.get("redirect_uris"))
        if not redirects:
            raise ClientError("invalid_client", "The client's metadata lists no redirect URIs.")
        name = meta.get("client_name")
        if not isinstance(name, str) or not name.strip():
            raise ClientError("invalid_client", "The client's metadata has no client_name.")
        client = found or OAuthClient(client_id=url, kind=ClientKind.METADATA_DOCUMENT)
        client.client_name = name.strip()[:200]
        client.client_uri = _https_or_none(meta.get("client_uri"))
        client.redirect_uris = redirects
        client.meta = {
            "grant_types": meta.get("grant_types", [GRANT_CODE, GRANT_REFRESH]),
            "logo_uri": _https_or_none(meta.get("logo_uri")),
        }
        client.fetched_at = now
        client.fresh_until = now + fresh
        if found is None:
            db.add(client)
        await db.flush()
        return client

    async def read_document(self, url: str) -> tuple[dict[str, Any], timedelta]:
        """The metadata document at ``url`` and how long it may be kept, checked."""
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.path in ("", "/")
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise ClientError(
                "invalid_client", "A client_id URL is https, with a path, and nothing more."
            )
        headers = {"Accept": "application/json"}
        extensions: dict[str, Any] = {}
        target = url
        if not self._allow_private:
            # Connect to the very address that was checked: resolving the name again to
            # connect could get another answer (DNS rebinding to a private address). TLS is
            # still checked against the hostname (SNI), and Host still names it.
            address = await _public_address(parts.hostname, parts.port or 443)
            netloc = f"[{address}]" if ":" in address else address
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            target = parts._replace(netloc=netloc).geturl()
            headers["Host"] = parts.netloc
            extensions["sni_hostname"] = parts.hostname
        http = self._http()
        if http is None:
            raise ClientError("invalid_client", "Client metadata documents can't be read here.")
        try:
            async with http.stream(
                "GET",
                target,
                headers=headers,
                extensions=extensions,
                follow_redirects=False,
                timeout=FETCH_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise ClientError(
                        "invalid_client", f"The client's metadata answered {response.status_code}."
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise ClientError("invalid_client", "The client's metadata is too large.")
                cache_control = response.headers.get("cache-control", "")
        except httpx.HTTPError:
            raise ClientError("invalid_client", "The client's metadata couldn't be read.") from None
        try:
            meta = json.loads(bytes(body))
        except ValueError:
            raise ClientError("invalid_client", "The client's metadata isn't JSON.") from None
        if not isinstance(meta, dict):
            raise ClientError("invalid_client", "The client's metadata isn't a JSON object.")
        doc: dict[str, Any] = meta  # pyright: ignore[reportUnknownVariableType]
        if doc.get("client_id") != url:
            raise ClientError(
                "invalid_client", "The client's metadata names another client_id than its URL."
            )
        _public_only(doc)
        return doc, _freshness(cache_control)


def _freshness(cache_control: str) -> timedelta:
    if "no-store" in cache_control or "no-cache" in cache_control:
        return MIN_FRESH
    found = _MAX_AGE.search(cache_control)
    if found is None:
        return DEFAULT_FRESH
    return min(MAX_FRESH, max(MIN_FRESH, timedelta(seconds=int(found.group(1)))))


async def _public_address(host: str, port: int) -> str:
    """The address to connect to for ``host``: refused if it resolves to any address that isn't
    public (the server's own network, loopback, link-local...)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ClientError("invalid_client", "The client's metadata host doesn't resolve.") from None
    addresses: Sequence[str] = [str(info[4][0]) for info in infos]
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise ClientError(
                "invalid_client", "The client's metadata URL points at a private address."
            )
    if not addresses:
        raise ClientError("invalid_client", "The client's metadata host doesn't resolve.")
    return addresses[0].split("%", 1)[0]
