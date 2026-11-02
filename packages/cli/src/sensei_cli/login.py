"""Signing in with the OAuth device flow (RFC 8628), and signing out.

The server's endpoints are found from its authorization server metadata (RFC 8414) and the
API's protected resource metadata (RFC 9728): the token is asked for that resource (RFC 8707),
the API, so it can't be used anywhere else.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx

from sensei_cli.api import CLIENT_ID, TIMEOUT, issued, unreachable
from sensei_cli.credentials import Tokens
from sensei_cli.errors import CliError

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPES = ("read:repo", "ask")
CONNECT = "connect:repo"
SLOW_DOWN = 5  # seconds added to the interval when told to slow down (RFC 8628 §3.5)

Json = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Server:
    """Where a server signs in, and the API resource its tokens are for."""

    issuer: str
    token_endpoint: str
    device_endpoint: str
    revocation_endpoint: str | None
    resource: str


@dataclass(frozen=True, slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


def _json(http: httpx.Client, url: str, server: str) -> Json:
    try:
        response = http.get(url)
    except httpx.TransportError as error:
        raise unreachable(server) from error
    if response.status_code != 200:
        raise CliError(
            f"{server} isn't a repo-sensei server (no sign-in metadata at {url}).",
            code="not_a_server",
        )
    try:
        return cast("Json", response.json())
    except ValueError as error:
        raise CliError(f"{server} answered something unexpected at {url}.") from error


def discover(http: httpx.Client, server: str) -> Server:
    found = _json(http, f"{server}/.well-known/oauth-authorization-server", server)
    resource = _json(http, f"{server}/.well-known/oauth-protected-resource/api", server)
    # RFC 8414 §3.3: the metadata is the server's own (no other issuer's, served from here).
    if str(found.get("issuer", "")).rstrip("/") != server:
        raise CliError(
            f"{server} signs in as {found.get('issuer')!r}, not itself.",
            hint=f"Sign in with `sensei login --server {found.get('issuer')}`.",
            code="issuer_mismatch",
        )
    device = found.get("device_authorization_endpoint")
    if not isinstance(device, str):
        raise CliError(f"{server} doesn't offer device sign-in.", code="not_a_server")
    return Server(
        issuer=str(found["issuer"]),
        token_endpoint=str(found["token_endpoint"]),
        device_endpoint=device,
        revocation_endpoint=found.get("revocation_endpoint"),
        resource=str(resource["resource"]),
    )


def _oauth_error(response: httpx.Response) -> tuple[str, str]:
    try:
        body = cast("Json", response.json())
    except ValueError:
        return "http_error", f"The server answered {response.status_code}."
    return str(body.get("error") or "http_error"), str(body.get("error_description") or "")


def start(http: httpx.Client, found: Server, scopes: list[str]) -> DeviceCode:
    response = http.post(
        found.device_endpoint,
        data={"client_id": CLIENT_ID, "scope": " ".join(scopes), "resource": found.resource},
    )
    if response.status_code != 200:
        error, said = _oauth_error(response)
        raise CliError(f"Couldn't start signing in: {said or error}.", code=error)
    body = cast("Json", response.json())
    return DeviceCode(
        device_code=str(body["device_code"]),
        user_code=str(body["user_code"]),
        verification_uri=str(body["verification_uri"]),
        verification_uri_complete=body.get("verification_uri_complete"),
        expires_in=int(body.get("expires_in") or 600),
        interval=int(body.get("interval") or 5),
    )


def wait(
    http: httpx.Client,
    found: Server,
    code: DeviceCode,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> Tokens:
    """Poll until the code is approved on the web (or denied, or expires)."""
    interval = code.interval
    ends = clock() + code.expires_in
    form = {"grant_type": DEVICE_GRANT, "client_id": CLIENT_ID, "device_code": code.device_code}
    while clock() < ends:
        sleep(interval)
        response = http.post(found.token_endpoint, data=form)
        if response.status_code == 200:
            like = Tokens(
                access_token="", refresh_token="", expires_at=0, scopes=[],
                token_endpoint=found.token_endpoint,
                revocation_endpoint=found.revocation_endpoint, resource=found.resource,
            )  # fmt: skip
            return issued(cast("Json", response.json()), like, now=clock())
        error, said = _oauth_error(response)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN
            continue
        if error == "access_denied":
            raise CliError("Signing in was declined.", code=error)
        if error == "expired_token":
            break
        raise CliError(f"Couldn't sign in: {said or error}.", code=error)
    raise CliError("The code expired before it was approved.", hint="Run `sensei login` again.",
                   code="expired_token")  # fmt: skip


def revoke(tokens: Tokens, *, transport: httpx.BaseTransport | None = None) -> bool:
    """Revoke the sign-in on the server (its refresh token: the whole family). False: the
    server couldn't be told (the sign-in is forgotten here all the same)."""
    if not tokens.revocation_endpoint:
        return False
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT) as http:
            response = http.post(tokens.revocation_endpoint, data={"token": tokens.refresh_token})
    except httpx.TransportError:
        return False
    return response.status_code == 200
