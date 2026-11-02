"""Connecting an editor (7.5): ``POST /orgs/{org}/mcp/test``, the settings page's "Test
connection".

It checks the MCP server as an editor meets it, here on the server:

* ``challenge``: ``/mcp`` without a credential answers 401 with a ``WWW-Authenticate`` pointing
  at its protected resource metadata (how an editor finds where to sign in);
* ``metadata``: that document names ``/mcp`` and this issuer;
* ``tools``: an MCP session (initialize, then ``tools/list``) lists the tools.

What the browser can reach on its own origin (the forwarded discovery document) the page checks
itself. Signing in is the editor's to do, so it isn't part of this.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal

import anyio
import httpx
from fastapi import APIRouter, Depends, Request
from mcp import Client
from pydantic import BaseModel

from sensei_api.auth import get_oauth_service
from sensei_api.mcp import MCP_PATH
from sensei_api.orgs import OrgAccessDep
from sensei_core.oauth.service import OAuthService

router = APIRouter(prefix="/orgs/{org}", tags=["mcp"])
TIMEOUT_S = 10
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
PROBE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "repo-sensei-check", "version": "1"}},
}  # fmt: skip


class McpCheck(BaseModel):
    name: Literal["challenge", "metadata", "tools"]
    ok: bool
    detail: str


class McpTestOut(BaseModel):
    ok: bool
    url: str
    server_name: str | None
    protocol_version: str | None
    tools: list[str]
    checks: list[McpCheck]
    ms: int


async def _challenge(http: httpx.AsyncClient, metadata_url: str) -> McpCheck:
    answer = await http.post(
        MCP_PATH, json=PROBE, headers={"Accept": "application/json, text/event-stream"}
    )
    if answer.status_code != 401:
        said = f"Without a sign-in it answered {answer.status_code}, not 401."
        return McpCheck(name="challenge", ok=False, detail=said)
    if f'resource_metadata="{metadata_url}"' not in answer.headers.get("www-authenticate", ""):
        return McpCheck(name="challenge", ok=False, detail="Its 401 doesn't say where to sign in.")
    return McpCheck(name="challenge", ok=True, detail="Asks editors to sign in, and says where.")


async def _metadata(http: httpx.AsyncClient, oauth: OAuthService) -> McpCheck:
    answer = await http.get(METADATA_PATH)
    if answer.status_code != 200:
        said = f"Its sign-in document answered {answer.status_code}."
        return McpCheck(name="metadata", ok=False, detail=said)
    found = answer.json()
    ours = (oauth.resources.mcp, [oauth.issuer])
    if (found.get("resource"), found.get("authorization_servers")) != ours:
        return McpCheck(
            name="metadata", ok=False, detail="Its sign-in document names another server."
        )
    return McpCheck(name="metadata", ok=True, detail=f"Sign-in is at {oauth.issuer}.")


@router.post("/mcp/test", response_model=McpTestOut)
async def check_connection(
    request: Request,
    access: OrgAccessDep,  # a member of the org (the page is theirs)
    oauth: Annotated[OAuthService, Depends(get_oauth_service)],
) -> McpTestOut:
    """Check the MCP server as an editor meets it: the sign-in challenge, the sign-in document,
    and a session listing the tools."""
    started = time.monotonic()
    checks: list[McpCheck] = []
    tools: list[str] = []
    server_name = protocol = None
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sensei.internal") as http:
        checks.append(await _challenge(http, f"{oauth.issuer}{METADATA_PATH}"))
        checks.append(await _metadata(http, oauth))
    try:
        with anyio.fail_after(TIMEOUT_S):
            async with Client(request.app.state.mcp_server) as client:
                listed = await client.list_tools()
                tools = sorted(tool.name for tool in listed.tools)
                server_name = client.server_info.name if client.server_info else None
                protocol = client.protocol_version
        found = McpCheck(name="tools", ok=bool(tools), detail=f"{len(tools)} tools.")
        checks.append(found)
    except Exception as error:  # reported as the check's failure, whatever it was
        said = f"The session failed: {type(error).__name__}."
        checks.append(McpCheck(name="tools", ok=False, detail=said))
    return McpTestOut(
        ok=all(check.ok for check in checks),
        url=oauth.resources.mcp,
        server_name=server_name,
        protocol_version=protocol,
        tools=tools,
        checks=checks,
        ms=round((time.monotonic() - started) * 1000),
    )
