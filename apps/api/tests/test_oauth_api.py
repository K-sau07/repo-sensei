"""OAuth 2.1 (7.2) over the API, as MCP clients and the CLI use it: discovery, registration
(dynamic, and by a metadata document), the authorization code flow with PKCE, rotating refresh
tokens (a reused one revokes its family), the device flow (``slow_down``, expiry), and tokens
held to their org, scopes and resource (audience)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.mail import MemoryMailer
from sensei_core.models import AuditEvent, Repo, RepoSource, User
from sensei_core.oauth.clients import ClientError, ClientRegistry
from sensei_core.oauth.service import OAuthService
from sensei_core.orgs.service import OrgService
from sensei_testing import make_user, sign_in

API = "/api/v1"
ISSUER = "http://localhost:3000"
MCP = f"{ISSUER}/mcp"
REST = f"{ISSUER}/api"
CALLBACK = "http://127.0.0.1:33418/callback"
DOC_URL = "https://client.example/oauth/client.json"
pytestmark = pytest.mark.integration


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _document(client_id: str = DOC_URL) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "client_name": "Example Editor",
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
    }


@pytest.fixture
async def clock(api_app: FastAPI) -> Clock:
    found = Clock()
    documents = {DOC_URL: _document(), "https://bad.example/c.json": _document("https://x/c")}

    def serve(request: httpx.Request) -> httpx.Response:
        doc = documents.get(str(request.url))
        if doc is None:
            return httpx.Response(404)
        return httpx.Response(200, json=doc, headers={"Cache-Control": "max-age=600"})

    outside = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    api_app.state.http = outside
    api_app.state.oauth_service = OAuthService(
        api_app.state.settings,
        ClientRegistry(lambda: outside, allow_private=True),
        clock=found,
    )
    return found


@pytest.fixture
async def client(api_app: FastAPI, fake_redis: FakeAsyncRedis) -> AsyncGenerator[httpx.AsyncClient]:
    api_app.state.redis = fake_redis
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


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def _register(client: httpx.AsyncClient) -> str:
    made = await client.post(
        f"{API}/oauth/register",
        json={"client_name": "Claude Code", "redirect_uris": [CALLBACK]},
    )
    assert made.status_code == 201, made.text
    return made.json()["client_id"]


async def _authorize(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    db: AsyncSession,
    owner: User,
    client_id: str,
    *,
    resource: str | None = REST,
    scope: str = "read:repo",
    redirect_uri: str = CALLBACK,
) -> tuple[str, str]:
    """The whole browser part: authorize, consent, back with a code. The code and verifier."""
    verifier, challenge = _pkce()
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": scope,
        "state": "xyz",
    }  # fmt: skip
    if resource:
        params["resource"] = resource
    asked = await client.get(f"{API}/oauth/authorize", params=params)
    assert asked.status_code == 302, asked.text
    handle = _query(asked.headers["location"])["request"]
    _, headers = await sign_in(api_app, client, db, owner)
    shown = await client.get(f"{API}/oauth/requests/{handle}")
    assert shown.status_code == 200, shown.text
    decided = await client.post(
        f"{API}/oauth/requests/{handle}/decision",
        json={"org": "acme", "approve": True},
        headers=headers,
    )
    assert decided.status_code == 200, decided.text
    back = _query(decided.json()["redirect_to"])
    assert (back["state"], back["iss"]) == ("xyz", ISSUER)
    client.cookies.clear()
    return back["code"], verifier


async def _token(client: httpx.AsyncClient, **form: str) -> httpx.Response:
    return await client.post(f"{API}/oauth/token", data=form)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_discovery_says_where_and_how(client: httpx.AsyncClient, clock: Clock) -> None:
    server = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert server["issuer"] == ISSUER
    assert server["authorization_endpoint"] == f"{ISSUER}{API}/oauth/authorize"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["client_id_metadata_document_supported"] is True
    assert server["authorization_response_iss_parameter_supported"] is True
    assert "urn:ietf:params:oauth:grant-type:device_code" in server["grant_types_supported"]
    for path, resource in (
        ("/.well-known/oauth-protected-resource", MCP),
        ("/.well-known/oauth-protected-resource/mcp", MCP),
        ("/.well-known/oauth-protected-resource/api", REST),
    ):
        found = (await client.get(path)).json()
        assert (found["resource"], found["authorization_servers"]) == (resource, [ISSUER])
        assert "offline_access" not in found["scopes_supported"]


async def test_registration_takes_public_clients_with_safe_redirects(
    client: httpx.AsyncClient, clock: Clock
) -> None:
    made = await client.post(
        f"{API}/oauth/register",
        json={"client_name": "Cursor", "redirect_uris": [CALLBACK, "https://app.example/cb"]},
    )
    assert made.status_code == 201
    assert made.json()["token_endpoint_auth_method"] == "none"
    assert made.json()["client_id"].startswith("rsc_")
    cases: list[tuple[dict[str, Any], str]] = [
        ({"redirect_uris": ["http://evil.example/cb"]}, "invalid_redirect_uri"),
        ({"redirect_uris": ["https://a.example/cb#x"]}, "invalid_redirect_uri"),
        ({"redirect_uris": []}, "invalid_redirect_uri"),
        (
            {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "client_secret_basic"},
            "invalid_client_metadata",
        ),
    ]
    for body, error in cases:
        bad = await client.post(f"{API}/oauth/register", json=body)
        assert (bad.status_code, bad.json()["error"]) == (400, error), body


async def test_the_code_flow_with_pkce_and_a_code_used_once(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    owner = await _org(db_session)
    client_id = await _register(client)
    # A loopback redirect may come back on another port (RFC 8252 §7.3).
    code, verifier = await _authorize(
        api_app, client, db_session, owner, client_id,
        redirect_uri="http://127.0.0.1:51000/callback",
    )  # fmt: skip
    common = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
              "redirect_uri": "http://127.0.0.1:51000/callback", "resource": REST}  # fmt: skip
    wrong = await _token(client, **common, code_verifier=_pkce()[0])
    assert (wrong.status_code, wrong.json()["error"]) == (400, "invalid_grant")
    got = await _token(client, **common, code_verifier=verifier)
    assert got.status_code == 200, got.text
    assert got.headers["cache-control"] == "no-store"
    tokens = got.json()
    assert (tokens["token_type"], tokens["scope"], tokens["expires_in"]) == (
        "Bearer", "read:repo", 3600,
    )  # fmt: skip
    access = tokens["access_token"]
    repos = await client.get(f"{API}/orgs/acme/repos", headers=_bearer(access))
    assert repos.status_code == 200, repos.text

    # The same code again: refused, and what it bought is revoked.
    again = await _token(client, **common, code_verifier=verifier)
    assert (again.status_code, again.json()["error"]) == (400, "invalid_grant")
    revoked = await client.get(f"{API}/orgs/acme/repos", headers=_bearer(access))
    assert (revoked.status_code, revoked.json()["error"]["code"]) == (401, "invalid_token")
    assert 'error="invalid_token"' in revoked.headers["www-authenticate"]


async def test_authorize_errors_go_back_only_to_a_known_redirect(
    client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    client_id = await _register(client)
    _, challenge = _pkce()
    base = {"client_id": client_id, "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "s1"}  # fmt: skip
    # Unknown redirect URI or client: shown on our page, the browser sent nowhere else.
    for params in (
        {**base, "redirect_uri": "https://evil.example/cb"},
        {**base, "client_id": "rsc_nope", "redirect_uri": CALLBACK},
    ):
        answer = await client.get(f"{API}/oauth/authorize", params=params)
        location = answer.headers["location"]
        assert location.startswith(f"{ISSUER}/oauth/consent?"), location
        assert "error=" in location
    # Known redirect URI: the error goes back there, with state and iss.
    for params, error in (
        ({**base, "redirect_uri": CALLBACK, "code_challenge_method": "plain"}, "invalid_request"),
        ({**base, "redirect_uri": CALLBACK, "scope": "admin"}, "invalid_scope"),
        ({**base, "redirect_uri": CALLBACK, "resource": "https://other.example"}, "invalid_target"),
    ):
        answer = await client.get(f"{API}/oauth/authorize", params=params)
        back = _query(answer.headers["location"])
        assert answer.headers["location"].startswith(CALLBACK)
        assert (back["error"], back["state"], back["iss"]) == (error, "s1", ISSUER)


async def test_a_denied_request_goes_back_as_access_denied(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    owner = await _org(db_session)
    client_id = await _register(client)
    _, challenge = _pkce()
    asked = await client.get(
        f"{API}/oauth/authorize",
        params={"client_id": client_id, "redirect_uri": CALLBACK, "response_type": "code",
                "code_challenge": challenge, "code_challenge_method": "S256"},
    )  # fmt: skip
    handle = _query(asked.headers["location"])["request"]
    _, headers = await sign_in(api_app, client, db_session, owner)
    shown = (await client.get(f"{API}/oauth/requests/{handle}")).json()
    assert shown["client_name"] == "Claude Code"
    assert (shown["redirect_host"], shown["redirect_is_local"]) == ("127.0.0.1:33418", True)
    assert (shown["scopes"], shown["resource"]) == (["read:repo"], MCP)  # the MCP server, unsaid
    no = await client.post(
        f"{API}/oauth/requests/{handle}/decision",
        json={"org": "acme", "approve": False},
        headers=headers,
    )
    assert _query(no.json()["redirect_to"])["error"] == "access_denied"
    twice = await client.post(
        f"{API}/oauth/requests/{handle}/decision",
        json={"org": "acme", "approve": True},
        headers=headers,
    )
    assert (twice.status_code, twice.json()["error"]["code"]) == (400, "oauth_invalid_request")


async def test_refresh_tokens_rotate_and_a_reused_one_revokes_its_family(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    owner = await _org(db_session)
    client_id = await _register(client)
    code, verifier = await _authorize(
        api_app, client, db_session, owner, client_id, scope="read:repo ask"
    )
    first = (
        await _token(
            client,
            grant_type="authorization_code",
            client_id=client_id,
            code=code,
            code_verifier=verifier,
            redirect_uri=CALLBACK,
        )
    ).json()
    refreshed = await _token(
        client, grant_type="refresh_token", client_id=client_id,
        refresh_token=first["refresh_token"], scope="read:repo",
    )  # fmt: skip
    assert refreshed.status_code == 200, refreshed.text
    second = refreshed.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["scope"] == "read:repo"  # narrowed
    widen = await _token(
        client, grant_type="refresh_token", client_id=client_id,
        refresh_token=second["refresh_token"], scope="read:repo ask",
    )  # fmt: skip
    assert widen.json()["error"] == "invalid_scope"
    # The first refresh token, again: stolen, so the whole family goes.
    reused = await _token(
        client, grant_type="refresh_token", client_id=client_id,
        refresh_token=first["refresh_token"],
    )  # fmt: skip
    assert (reused.status_code, reused.json()["error"]) == (400, "invalid_grant")
    gone = await client.get(f"{API}/orgs/acme/repos", headers=_bearer(second["access_token"]))
    assert gone.status_code == 401
    after = await _token(
        client, grant_type="refresh_token", client_id=client_id,
        refresh_token=second["refresh_token"],
    )  # fmt: skip
    assert after.json()["error"] == "invalid_grant"
    actions = (await db_session.scalars(select(AuditEvent.action))).all()
    assert "oauth.authorize" in actions
    assert "oauth.refresh_reused" in actions


async def test_a_token_is_for_its_resource_scopes_and_until_revoked(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    owner = await _org(db_session)
    client_id = await _register(client)
    # One for the MCP server: the API refuses it (RFC 8707: not its audience).
    code, verifier = await _authorize(api_app, client, db_session, owner, client_id, resource=MCP)
    for_mcp = (
        await _token(
            client,
            grant_type="authorization_code",
            client_id=client_id,
            code=code,
            code_verifier=verifier,
        )
    ).json()["access_token"]
    elsewhere = await client.get(f"{API}/orgs/acme/repos", headers=_bearer(for_mcp))
    assert elsewhere.status_code == 401
    assert "resource_metadata=" in elsewhere.headers["www-authenticate"]
    # One for the API, reading only: asking is out of its scope.
    code, verifier = await _authorize(api_app, client, db_session, owner, client_id)
    reader = (
        await _token(
            client,
            grant_type="authorization_code",
            client_id=client_id,
            code=code,
            code_verifier=verifier,
        )
    ).json()["access_token"]
    asked = await client.post(
        f"{API}/orgs/acme/repos/items/ask", json={"question": "?"}, headers=_bearer(reader)
    )
    assert (asked.status_code, asked.json()["error"]["code"]) == (403, "insufficient_scope")
    challenge = asked.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="ask"' in challenge
    # Expired, then revoked.
    clock.now += timedelta(hours=2)
    late = await client.get(f"{API}/orgs/acme/repos", headers=_bearer(reader))
    assert late.status_code == 401
    clock.now -= timedelta(hours=2)
    assert (await client.post(f"{API}/oauth/revoke", data={"token": reader})).status_code == 200
    assert (await client.get(f"{API}/orgs/acme/repos", headers=_bearer(reader))).status_code == 401
    assert (await client.post(f"{API}/oauth/revoke", data={"token": "nope"})).status_code == 200


async def test_the_device_flow_waits_slows_down_and_expires(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    owner = await _org(db_session)
    started = await client.post(f"{API}/oauth/device/code", data={"scope": "read:repo ask"})
    assert started.status_code == 200, started.text
    device = started.json()
    assert device["verification_uri"] == f"{ISSUER}/device"
    assert device["interval"] == 5
    poll = {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": "sensei-cli", "device_code": device["device_code"]}  # fmt: skip
    assert (await _token(client, **poll)).json()["error"] == "authorization_pending"
    assert (await _token(client, **poll)).json()["error"] == "slow_down"  # too soon
    clock.now += timedelta(seconds=11)

    _, headers = await sign_in(api_app, client, db_session, owner)
    typed = device["user_code"].lower().replace("-", " ")  # as a person types it
    shown = await client.get(f"{API}/oauth/device/{typed}")
    assert (shown.json()["client_name"], shown.json()["scopes"]) == (
        "repo-sensei CLI", ["ask", "read:repo"],
    )  # fmt: skip
    ok = await client.post(
        f"{API}/oauth/device/decision",
        json={"user_code": typed, "org": "acme", "approve": True},
        headers=headers,
    )
    assert ok.status_code == 204, ok.text
    client.cookies.clear()
    got = await _token(client, **poll)
    assert got.status_code == 200, got.text
    access = got.json()["access_token"]
    assert (await client.get(f"{API}/me", headers=_bearer(access))).json()["login"] == owner.login
    assert (await _token(client, **poll)).json()["error"] == "invalid_grant"  # once

    late = (await client.post(f"{API}/oauth/device/code")).json()
    clock.now += timedelta(minutes=11)
    expired = await _token(client, **{**poll, "device_code": late["device_code"]})
    assert expired.json()["error"] == "expired_token"


async def test_a_client_known_by_its_metadata_document(
    api_app: FastAPI, client: httpx.AsyncClient, db_session: AsyncSession, clock: Clock
) -> None:
    owner = await _org(db_session)
    code, verifier = await _authorize(api_app, client, db_session, owner, DOC_URL)
    got = await _token(
        client, grant_type="authorization_code", client_id=DOC_URL, code=code,
        code_verifier=verifier, redirect_uri=CALLBACK,
    )  # fmt: skip
    assert got.status_code == 200, got.text
    # A document naming another client_id than its URL: refused, on our page.
    _, challenge = _pkce()
    bad = await client.get(
        f"{API}/oauth/authorize",
        params={"client_id": "https://bad.example/c.json", "redirect_uri": CALLBACK,
                "response_type": "code", "code_challenge": challenge,
                "code_challenge_method": "S256"},
    )  # fmt: skip
    assert _query(bad.headers["location"])["error"] == "invalid_client"


async def test_metadata_documents_are_never_fetched_from_private_addresses() -> None:
    registry = ClientRegistry(lambda: None)
    for url in (
        "https://127.0.0.1/client.json",
        "https://10.0.0.8/client.json",
        "https://[::1]/client.json",
        "https://169.254.169.254/latest/meta-data",
        "http://client.example/client.json",
        "https://client.example/",
    ):
        with pytest.raises(ClientError) as found:
            await registry.read_document(url)
        assert found.value.error == "invalid_client", url
    assert json.dumps(_document())  # (the documents above are well-formed)
