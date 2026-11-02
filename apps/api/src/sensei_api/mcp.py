"""The MCP server over Streamable HTTP at ``/mcp`` (7.3), for editors to connect to.

``/mcp`` is a resource of its own (RFC 8707): it takes OAuth access tokens issued for it (and
API keys, for clients configured with a header), checked here before the MCP SDK sees the
request. No credential, or a bad one: 401 with ``WWW-Authenticate`` pointing at the protected
resource metadata (RFC 9728), which is how a client finds where to sign in. A token without
``read:repo``, or calling ``sensei_ask`` without ``ask``: 403 ``insufficient_scope`` naming the
scopes, so the client can sign in again with them.

Stateless (each request stands alone; the repository is named per call) and answering in JSON.
The tools call the API itself, in-process, with the caller's credential: a request marked
``INTERNAL_MCP`` in its ASGI scope (see :mod:`sensei_api.auth`; nothing from outside can set it)
is the one place an ``/mcp`` token is taken by the API's routes.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from sensei_api.auth import (
    INTERNAL_MCP,
    KEY_PREFIX,
    Principal,
    get_api_key_service,
    get_oauth_service,
)
from sensei_api.deps import DbSession
from sensei_api.routers.webhooks import read_body
from sensei_core.auth.api_keys import ApiKeyService, KeyRejected
from sensei_core.auth.api_keys import Scope as KeyScope
from sensei_core.oauth.service import ACCESS_PREFIX, OAuthService, TokenRejected
from sensei_mcp.api import SenseiApi
from sensei_mcp.server import SenseiServer, build_server

MCP_PATH = "/mcp"
MAX_BODY = 1024 * 1024
ASK_TOOLS = frozenset({"sensei_ask"})

router = APIRouter(include_in_schema=False)


def internal(app: ASGIApp) -> ASGIApp:
    """``app``, with each request marked as the MCP tools' own."""

    async def call(scope: Scope, receive: Receive, send: Send) -> None:
        await app({**scope, INTERNAL_MCP: True}, receive, send)

    return call


def build(app: FastAPI) -> SenseiServer:
    """The MCP server, its tools calling ``app`` in-process with the caller's credential."""
    transport = httpx.ASGITransport(app=internal(app))

    async def api_for(ctx: Context) -> SenseiApi:
        headers = ctx.headers or {}
        authorization = headers.get("authorization") or headers.get("Authorization") or ""
        return SenseiApi("http://sensei.internal", authorization, transport=transport)

    return build_server(api_for, close_after=True)


def http_app(server: SenseiServer) -> ASGIApp:
    # The bearer check below is the gate: DNS-rebinding protection (for servers that trust any
    # local caller) adds nothing to a server that takes no cookies and needs a token.
    return server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


class _Forward(Response):
    """Hands the request (its body, already read) on to the MCP app."""

    def __init__(self, app: ASGIApp, body: bytes) -> None:
        super().__init__()
        self._app = app
        self._body = body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        given = False

        async def replay() -> dict[str, Any]:
            nonlocal given
            if not given:
                given = True
                return {"type": "http.request", "body": self._body, "more_body": False}
            return cast("dict[str, Any]", await receive())

        await self._app(scope, replay, send)


def _challenge(
    oauth: OAuthService, status: int, error: str, description: str, **extra: str
) -> JSONResponse:
    """RFC 6750's challenge, pointing at ``/mcp``'s protected resource metadata (RFC 9728)."""
    metadata = f"{oauth.issuer}/.well-known/oauth-protected-resource/mcp"
    params = {"error": error, "error_description": description, **extra}
    params["resource_metadata"] = metadata
    challenge = "Bearer " + ", ".join(f'{k}="{v}"' for k, v in params.items())
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"WWW-Authenticate": challenge},
    )


def _calls(body: bytes) -> set[str]:
    """The tools a JSON-RPC message (or batch) calls."""
    try:
        parsed: Any = json.loads(body) if body else None
    except ValueError:
        return set()
    messages = cast("list[Any]", parsed if isinstance(parsed, list) else [parsed])
    names: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        found = cast("dict[str, Any]", message)
        params = found.get("params")
        if found.get("method") == "tools/call" and isinstance(params, dict):
            name = cast("dict[str, Any]", params).get("name")
            if isinstance(name, str):
                names.add(name)
    return names


@router.api_route(MCP_PATH, methods=["GET", "POST", "DELETE"])
async def mcp(
    request: Request,
    db: DbSession,
    keys: Annotated[ApiKeyService, Depends(get_api_key_service)],
    oauth: Annotated[OAuthService, Depends(get_oauth_service)],
) -> Response:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return _challenge(oauth, 401, "invalid_token", "Sign in to use repo-sensei.",
                          scope="read:repo")  # fmt: skip
    principal: Principal
    try:
        if token.startswith(ACCESS_PREFIX):
            principal = await oauth.authenticate(db, token, resource=oauth.resources.mcp)
        elif token.startswith(KEY_PREFIX):
            principal = await keys.authenticate(db, token)
        else:
            raise TokenRejected("invalid")
    except (TokenRejected, KeyRejected) as rejected:
        return _challenge(oauth, 401, "invalid_token", f"The credential is {rejected.reason}.")
    if not principal.allows(KeyScope.READ_REPO):
        return _challenge(oauth, 403, "insufficient_scope", "Reading needs read:repo.",
                          scope="read:repo")  # fmt: skip
    body = await read_body(request, MAX_BODY)  # JSON-RPC messages are small
    if _calls(body) & ASK_TOOLS and not principal.allows(KeyScope.ASK):
        return _challenge(oauth, 403, "insufficient_scope", "Asking needs the ask scope.",
                          scope="read:repo ask")  # fmt: skip
    inner: ASGIApp = request.app.state.mcp_http
    return _Forward(inner, body)
