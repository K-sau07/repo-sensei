"""Sign in with GitHub, end to end against a mocked GitHub."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.auth import cookie_names
from sensei_api.routers.auth import safe_next
from sensei_core.auth.github_login import fresh_access_token, store_tokens
from sensei_core.config import GitHubSettings, Settings
from sensei_core.github.oauth import GitHubOAuth, GitHubTokens, OAuthError
from sensei_core.models import OAuthToken, User
from sensei_core.security.crypto import TokenCipher
from sensei_testing import make_user

API = "/api/v1"
GITHUB = "https://github.com"
GITHUB_API = "https://api.github.com"
GH_SETTINGS = GitHubSettings(client_id="Iv1.test", client_secret=SecretStr("shh"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/acme/langflow?tab=ask", "/acme/langflow?tab=ask"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/x", "/"),
        ("javascript:alert(1)", "/"),
        ("/ok\nSet-Cookie: x", "/"),
        ("relative/path", "/"),
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    assert safe_next(value) == expected


def _github_mocks(
    router: respx.MockRouter, *, token_body: dict[str, object] | None = None
) -> respx.Route:
    token_route = router.post(f"{GITHUB}/login/oauth/access_token").mock(
        return_value=httpx.Response(
            200,
            json=token_body
            or {
                "access_token": "ghu_access_1",
                "expires_in": 28800,
                "refresh_token": "ghr_refresh_1",
                "refresh_token_expires_in": 15811200,
                "scope": "",
                "token_type": "bearer",
            },
        )
    )
    router.get(f"{GITHUB_API}/user").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 4242,
                "login": "octo",
                "name": "Octo Cat",
                "email": None,
                "avatar_url": "https://avatars.example/4242",
            },
        )
    )
    router.get(f"{GITHUB_API}/user/emails").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"email": "old@example.com", "primary": False, "verified": True},
                {"email": "octo@example.com", "primary": True, "verified": True},
            ],
        )
    )
    return token_route


# ------------------------------------------------------------------- unit: oauth client


async def test_start_uses_pkce_and_stores_state(fake_redis: FakeAsyncRedis) -> None:
    async with httpx.AsyncClient() as http:
        oauth = GitHubOAuth(GH_SETTINGS, fake_redis, http, redirect_uri="https://app/cb")
        url = urlsplit(await oauth.start("/acme"))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "github.com"
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == "https://app/cb"
    stored = await fake_redis.get(f"oauth:github:state:{query['state']}")
    assert stored is not None
    assert '"next": "/acme"' in str(stored)


async def test_state_is_single_use(fake_redis: FakeAsyncRedis) -> None:
    async with httpx.AsyncClient() as http, respx.mock(assert_all_called=False) as router:
        _github_mocks(router)
        oauth = GitHubOAuth(GH_SETTINGS, fake_redis, http, redirect_uri="https://app/cb")
        state = parse_qs(urlsplit(await oauth.start("/")).query)["state"][0]
        await oauth.finish("code-1", state)
        with pytest.raises(OAuthError) as exc:
            await oauth.finish("code-1", state)
    assert exc.value.code == "invalid_state"


async def test_pkce_verifier_matches_challenge(fake_redis: FakeAsyncRedis) -> None:
    async with httpx.AsyncClient() as http, respx.mock(assert_all_called=False) as router:
        token_route = _github_mocks(router)
        oauth = GitHubOAuth(GH_SETTINGS, fake_redis, http, redirect_uri="https://app/cb")
        query = parse_qs(urlsplit(await oauth.start("/")).query)
        await oauth.finish("code-1", query["state"][0])
    sent = parse_qs(token_route.calls.last.request.content.decode())
    verifier = sent["code_verifier"][0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert query["code_challenge"][0] == expected.decode()


async def test_token_errors_are_reported(fake_redis: FakeAsyncRedis) -> None:
    async with httpx.AsyncClient() as http, respx.mock(assert_all_called=False) as router:
        _github_mocks(router, token_body={"error": "bad_verification_code"})
        oauth = GitHubOAuth(GH_SETTINGS, fake_redis, http, redirect_uri="https://app/cb")
        state = parse_qs(urlsplit(await oauth.start("/")).query)["state"][0]
        with pytest.raises(OAuthError) as exc:
            await oauth.finish("bad", state)
    assert exc.value.code == "bad_verification_code"


async def test_email_falls_back_to_profile_when_emails_unavailable(
    fake_redis: FakeAsyncRedis,
) -> None:
    async with httpx.AsyncClient() as http, respx.mock(assert_all_called=False) as router:
        router.get(f"{GITHUB_API}/user").mock(
            return_value=httpx.Response(
                200, json={"id": 1, "login": "x", "email": "public@example.com"}
            )
        )
        router.get(f"{GITHUB_API}/user/emails").mock(return_value=httpx.Response(403))
        oauth = GitHubOAuth(GH_SETTINGS, fake_redis, http, redirect_uri="https://app/cb")
        user = await oauth.fetch_user("token")
    assert user.email == "public@example.com"


def test_unconfigured_github_is_refused(fake_redis: FakeAsyncRedis) -> None:
    with pytest.raises(OAuthError) as exc:
        GitHubOAuth(GitHubSettings(), fake_redis, httpx.AsyncClient(), redirect_uri="x")
    assert exc.value.code == "github_not_configured"


# ------------------------------------------------------------------- http flow


@pytest.fixture
def github_app(api_app: FastAPI, migrated_test_db: Settings, fake_redis: FakeAsyncRedis) -> FastAPI:
    api_app.state.settings = migrated_test_db.model_copy(update={"github": GH_SETTINGS})
    api_app.state.redis = fake_redis
    return api_app


@pytest.fixture
async def github_client(github_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        github_app.state.http = http
        transport = httpx.ASGITransport(app=github_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def _start_login(client: httpx.AsyncClient, next_path: str) -> str:
    response = await client.get(f"{API}/auth/github/login", params={"next": next_path})
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


@pytest.mark.integration
async def test_full_sign_in_flow(
    github_app: FastAPI, github_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    state = await _start_login(github_client, "/acme/langflow")
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as router:
        router.route(host="testserver").pass_through()
        _github_mocks(router)
        response = await github_client.get(
            f"{API}/auth/github/callback", params={"code": "c1", "state": state}
        )

    origin = github_app.state.settings.web.origin
    assert response.status_code == 302
    assert response.headers["location"] == f"{origin}/auth/callback?next=/acme/langflow"

    user = await db_session.scalar(select(User).where(User.github_id == 4242))
    assert user is not None
    assert (user.login, user.email) == ("octo", "octo@example.com")

    token_row = await db_session.scalar(select(OAuthToken).where(OAuthToken.user_id == user.id))
    assert token_row is not None
    assert b"ghu_access_1" not in token_row.access_token_enc
    cipher: TokenCipher = github_app.state.token_cipher
    assert cipher.decrypt_text(token_row.access_token_enc, context=str(user.id)) == "ghu_access_1"

    session_cookie, _ = cookie_names(github_app.state.settings)
    set_cookies = response.headers.get_list("set-cookie")
    session_value = next(c for c in set_cookies if c.startswith(f"{session_cookie}="))
    assert "HttpOnly" in session_value
    token = session_value.split(";", 1)[0].split("=", 1)[1]
    github_client.cookies.set(session_cookie, token)
    me = await github_client.get(f"{API}/me")
    assert me.status_code == 200
    assert me.json()["login"] == "octo"


@pytest.mark.integration
async def test_second_sign_in_updates_profile_without_duplicates(
    github_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, github_id=4242, login="old-login")
    state = await _start_login(github_client, "/")
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as router:
        router.route(host="testserver").pass_through()
        _github_mocks(router)
        await github_client.get(f"{API}/auth/github/callback", params={"code": "c", "state": state})
    users = (await db_session.scalars(select(User).where(User.github_id == 4242))).all()
    assert [u.login for u in users] == ["octo"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("params", "expected_error"),
    [
        ({"error": "access_denied", "state": "x"}, "access_denied"),
        ({"code": "c", "state": "never-issued"}, "invalid_state"),
        ({}, "oauth_failed"),
    ],
)
async def test_failed_callbacks_redirect_to_login(
    github_app: FastAPI,
    github_client: httpx.AsyncClient,
    params: dict[str, str],
    expected_error: str,
) -> None:
    response = await github_client.get(f"{API}/auth/github/callback", params=params)
    origin = github_app.state.settings.web.origin
    assert response.status_code == 302
    assert response.headers["location"] == f"{origin}/login?error={expected_error}"
    assert "set-cookie" not in response.headers


@pytest.mark.integration
async def test_open_redirects_are_neutralized(
    github_client: httpx.AsyncClient, fake_redis: FakeAsyncRedis
) -> None:
    state = await _start_login(github_client, "https://evil.example/steal")
    stored = await fake_redis.get(f"oauth:github:state:{state}")
    assert stored is not None
    assert '"next": "/"' in str(stored)


@pytest.mark.integration
async def test_login_when_github_is_not_configured(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get(f"{API}/auth/github/login")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "github_not_configured"


# ------------------------------------------------------------------- token refresh


@pytest.mark.integration
async def test_fresh_access_token_refreshes_when_expiring(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    cipher = TokenCipher(b"k" * 32)
    user = await make_user(db_session)
    now = datetime(2026, 10, 18, tzinfo=UTC)
    await store_tokens(
        db_session,
        cipher,
        user,
        GitHubTokens(
            "ghu_old", "ghr_old", now + timedelta(minutes=2), now + timedelta(days=90), []
        ),
    )
    async with httpx.AsyncClient() as http, respx.mock(assert_all_called=False) as router:
        refresh_route = _github_mocks(
            router,
            token_body={
                "access_token": "ghu_new",
                "refresh_token": "ghr_new",
                "expires_in": 28800,
                "refresh_token_expires_in": 15811200,
            },
        )
        oauth = GitHubOAuth(GH_SETTINGS, fake_redis, http, redirect_uri="https://app/cb")
        token = await fresh_access_token(db_session, cipher, oauth, user, now=now)
    assert token == "ghu_new"
    assert parse_qs(refresh_route.calls.last.request.content.decode())["grant_type"] == [
        "refresh_token"
    ]


@pytest.mark.integration
async def test_fresh_access_token_requires_reauth_when_refresh_expired(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    cipher = TokenCipher(b"k" * 32)
    user = await make_user(db_session)
    now = datetime(2026, 10, 18, tzinfo=UTC)
    await store_tokens(
        db_session,
        cipher,
        user,
        GitHubTokens("ghu_old", "ghr_old", now - timedelta(hours=1), now - timedelta(days=1), []),
    )
    async with httpx.AsyncClient() as http:
        oauth = GitHubOAuth(GH_SETTINGS, fake_redis, http, redirect_uri="https://app/cb")
        with pytest.raises(OAuthError) as exc:
            await fresh_access_token(db_session, cipher, oauth, user, now=now)
    assert exc.value.code == "github_reauth_required"
