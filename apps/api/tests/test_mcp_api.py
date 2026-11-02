"""The MCP server over Streamable HTTP at ``/mcp`` (7.3), as an editor reaches it: a 401 that
says where to sign in, tokens taken only when issued for ``/mcp`` (or API keys), the tools
answering from the API in-process with the caller's rights, and scopes asked for when missing."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import Repo, RepoSource, User
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

API = "/api/v1"
ISSUER = "http://localhost:3000"
MCP = f"{ISSUER}/mcp"
DEVICE = "urn:ietf:params:oauth:grant-type:device_code"
PROTOCOL = "2025-06-18"
pytestmark = pytest.mark.integration


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
    async with httpx.AsyncClient() as outside:
        api_app.state.http = outside
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def _org(db: AsyncSession) -> User:
    owner = await make_user(db)
    access = await OrgService(MemoryMailer(), ISSUER).create(db, owner, name="Acme", slug="acme")
    db.add(
        Repo(
            org_id=access.org.id, source=RepoSource.PUBLIC_URL, github_repo_id=7, owner="o",
            name="items", full_name="o/items", slug="items", default_branch="main",
            visibility="public",
        )
    )  # fmt: skip
    await db.flush()
    return owner


async def _token(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    db: AsyncSession,
    owner: User,
    *,
    scope: str,
    resource: str,
) -> str:
    """An access token, by the device flow (the code allowed on the web)."""
    device = (
        await client.post(f"{API}/oauth/device/code", data={"scope": scope, "resource": resource})
    ).json()
    _, headers = await sign_in(api_app, client, db, owner)
    allowed = await client.post(
        f"{API}/oauth/device/decision",
        json={"user_code": device["user_code"], "org": "acme", "approve": True},
        headers=headers,
    )
    assert allowed.status_code == 204, allowed.text
    client.cookies.clear()
    got = await client.post(
        f"{API}/oauth/token",
        data={
            "grant_type": DEVICE,
            "client_id": "sensei-cli",
            "device_code": device["device_code"],
        },
    )
    assert got.status_code == 200, got.text
    return got.json()["access_token"]


async def _rpc(
    client: httpx.AsyncClient,
    credential: str | None,
    method: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
    }
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return await client.post("/mcp", json=message, headers=headers)


INITIALIZE = {
    "protocolVersion": PROTOCOL,
    "capabilities": {},
    "clientInfo": {"name": "test-editor", "version": "1"},
}


async def test_without_a_credential_it_says_where_to_sign_in(
    api_app: FastAPI, client: httpx.AsyncClient
) -> None:
    # The MCP app's sessions, entered and left in this test's own task (anyio's rule).
    async with api_app.state.mcp_server.session_manager.run():
        answer = await _rpc(client, None, "initialize", INITIALIZE)
        assert answer.status_code == 401
        challenge = answer.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"' in challenge
        # Where it points: this resource, and who issues tokens for it.
        found = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert (found["resource"], found["authorization_servers"]) == (MCP, [ISSUER])


async def test_a_token_for_mcp_reaches_the_tools_and_they_reach_the_api(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # The MCP app's sessions, entered and left in this test's own task (anyio's rule).
    async with api_app.state.mcp_server.session_manager.run():
        owner = await _org(db_session)
        token = await _token(api_app, client, db_session, owner, scope="read:repo", resource=MCP)
        started = await _rpc(client, token, "initialize", INITIALIZE)
        assert started.status_code == 200, started.text
        assert started.json()["result"]["serverInfo"]["name"] == "repo-sensei"
        listed = (await _rpc(client, token, "tools/list")).json()["result"]["tools"]
        assert len(listed) == 8
        called = (
            await _rpc(client, token, "tools/call", {"name": "sensei_list_repos", "arguments": {}})
        ).json()["result"]
        assert not called.get("isError"), called
        assert called["structuredContent"]["org"] == "acme"
        assert [r["slug"] for r in called["structuredContent"]["repos"]] == ["items"]
        # The same token from outside, straight at the API: not its audience.
        outside = await client.get(
            f"{API}/orgs/acme/repos", headers={"Authorization": f"Bearer {token}"}
        )
        assert outside.status_code == 401


async def test_tokens_for_another_resource_or_scope_are_refused(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # The MCP app's sessions, entered and left in this test's own task (anyio's rule).
    async with api_app.state.mcp_server.session_manager.run():
        owner = await _org(db_session)
        for_api = await _token(
            api_app, client, db_session, owner, scope="read:repo", resource=f"{ISSUER}/api"
        )
        refused = await _rpc(client, for_api, "initialize", INITIALIZE)
        assert refused.status_code == 401
        assert 'error="invalid_token"' in refused.headers["www-authenticate"]
        # Reading only: asking is refused before the tool runs, naming the scopes to sign in with.
        reader = await _token(api_app, client, db_session, owner, scope="read:repo", resource=MCP)
        asked = await _rpc(
            client, reader, "tools/call",
            {"name": "sensei_ask", "arguments": {"repo": "items", "question": "how?"}},
        )  # fmt: skip
        assert asked.status_code == 403
        challenge = asked.headers["www-authenticate"]
        assert 'error="insufficient_scope"' in challenge
        assert 'scope="read:repo ask"' in challenge


async def test_an_api_key_works_too(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # The MCP app's sessions, entered and left in this test's own task (anyio's rule).
    async with api_app.state.mcp_server.session_manager.run():
        owner = await _org(db_session)
        _, headers = await sign_in(api_app, client, db_session, owner)
        made = await client.post(
            f"{API}/orgs/acme/api-keys",
            json={"name": "editor", "scopes": ["read:repo"]},
            headers=headers,
        )
        key = made.json()["key"]
        client.cookies.clear()
        called = (
            await _rpc(client, key, "tools/call", {"name": "sensei_list_repos", "arguments": {}})
        ).json()["result"]
        assert called["structuredContent"]["repos"][0]["slug"] == "items"
        assert (await _rpc(client, "rs_nope", "tools/list")).status_code == 401


async def test_the_settings_page_checks_the_server_as_an_editor_meets_it(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _org(db_session)
    _, headers = await sign_in(api_app, client, db_session, owner)
    checked = await client.post(f"{API}/orgs/acme/mcp/test", headers=headers)
    assert checked.status_code == 200, checked.text
    found = checked.json()
    assert found["ok"] is True, found["checks"]
    assert [c["name"] for c in found["checks"]] == ["challenge", "metadata", "tools"]
    assert found["url"] == MCP
    assert found["server_name"] == "repo-sensei"
    assert len(found["tools"]) == 8
    assert "sensei_ask" in found["tools"]
    # A page's: keys and tokens can't call it; another org's members can't either.
    key = (
        await client.post(
            f"{API}/orgs/acme/api-keys",
            json={"name": "k", "scopes": ["read:repo"]},
            headers=headers,
        )
    ).json()["key"]
    client.cookies.clear()
    refused = await client.post(
        f"{API}/orgs/acme/mcp/test", headers={"Authorization": f"Bearer {key}"}
    )
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "api_key_not_allowed")
    stranger = await make_user(db_session)
    _, theirs = await sign_in(api_app, client, db_session, stranger)
    assert (await client.post(f"{API}/orgs/acme/mcp/test", headers=theirs)).status_code == 404


async def test_an_oversized_message_is_refused(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    async with api_app.state.mcp_server.session_manager.run():
        owner = await _org(db_session)
        token = await _token(api_app, client, db_session, owner, scope="read:repo", resource=MCP)
        message = b'{"jsonrpc": "2.0", "id": 1, "method": "x", "params": "' + b"a" * (2 << 20)
        huge = await client.post(
            "/mcp",
            content=message + b'"}',
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert huge.status_code == 413
