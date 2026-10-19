"""Acting as the GitHub App: JWT claims and cached, encrypted installation tokens."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import jwt
import pytest
import respx
import respx.models
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr

from sensei_core.config import AuthSettings, GitHubSettings
from sensei_core.github.app import GitHubApp, GitHubAppNotConfiguredError
from sensei_core.github.client import GitHubClient, GitHubError
from sensei_core.security.crypto import TokenCipher

API = "https://api.github.com"
INSTALLATION = 4242
TOKENS_URL = f"{API}/app/installations/{INSTALLATION}/access_tokens"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
SETTINGS = GitHubSettings(
    app_id=99,
    app_slug="repo-sensei-test",
    client_id="Iv23.test",
    private_key=SecretStr(PRIVATE_PEM),
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def http() -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def _app(
    redis: FakeAsyncRedis,
    http: httpx.AsyncClient,
    clock: Clock,
    settings: GitHubSettings = SETTINGS,
) -> GitHubApp:
    client = GitHubClient(settings, http, sleep=_no_sleep, now=clock)
    return GitHubApp(settings, client, redis, TokenCipher.from_settings(AuthSettings()), now=clock)


def _token_response(
    clock: Clock,
    token: str = "ghs_one",  # noqa: S107 - a fake token
    minutes: int = 60,
) -> httpx.Response:
    expires = (clock.now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return httpx.Response(
        201,
        json={
            "token": token,
            "expires_at": expires,
            "permissions": {"contents": "read", "metadata": "read"},
            "repository_selection": "selected",
        },
    )


# ------------------------------------------------------------------ app JWT


def test_app_jwt_claims(fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock) -> None:
    token = _app(fake_redis, http, clock).app_jwt()
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        token, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    now = int(clock.now.timestamp())
    assert header["alg"] == "RS256"
    assert claims == {"iat": now - 60, "exp": now + 540, "iss": "Iv23.test"}
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's maximum


def test_app_jwt_falls_back_to_the_app_id(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    settings = SETTINGS.model_copy(update={"client_id": None})
    claims = jwt.decode(
        _app(fake_redis, http, clock, settings).app_jwt(),
        PUBLIC_PEM,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "99"


def test_app_jwt_is_reused_until_close_to_expiry(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    first = app.app_jwt()
    clock.now += timedelta(minutes=7)
    assert app.app_jwt() == first
    clock.now += timedelta(minutes=1, seconds=1)  # under a minute left
    assert app.app_jwt() != first


def test_private_key_can_come_from_a_file(
    tmp_path: Path, fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    path = tmp_path / "app.pem"
    path.write_text(PRIVATE_PEM)
    settings = SETTINGS.model_copy(update={"private_key": None, "private_key_path": str(path)})
    jwt.decode(
        _app(fake_redis, http, clock, settings).app_jwt(),
        PUBLIC_PEM,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )


def test_unconfigured_app_is_refused(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    for settings in (GitHubSettings(), SETTINGS.model_copy(update={"private_key": None})):
        with pytest.raises(GitHubAppNotConfiguredError):
            _app(fake_redis, http, clock, settings)


# ------------------------------------------------------------------ installation tokens


async def test_installation_token_is_minted_with_the_app_jwt(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        route = router.post(TOKENS_URL).mock(return_value=_token_response(clock))
        token = await app.installation_token(INSTALLATION)
    assert token.token == "ghs_one"
    assert token.expires_at == clock.now + timedelta(hours=1)
    assert token.permissions == {"contents": "read", "metadata": "read"}
    sent = route.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {app.app_jwt()}"
    assert sent.content in (b"", b"null")  # an unrestricted token sends no body


async def test_tokens_are_cached_until_five_minutes_before_expiry(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        route = router.post(TOKENS_URL).mock(
            side_effect=[_token_response(clock, "ghs_one"), _token_response(clock, "ghs_two")]
        )
        assert (await app.installation_token(INSTALLATION)).token == "ghs_one"
        clock.now += timedelta(minutes=54)
        assert (await app.installation_token(INSTALLATION)).token == "ghs_one"
        assert route.call_count == 1
        clock.now += timedelta(minutes=1, seconds=1)  # inside the 5-minute margin
        assert (await app.installation_token(INSTALLATION)).token == "ghs_two"
    assert route.call_count == 2


async def test_cache_ttl_is_expiry_minus_five_minutes(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        router.post(TOKENS_URL).mock(return_value=_token_response(clock))
        await app.installation_token(INSTALLATION)
    key = f"github:itoken:{INSTALLATION}:all"
    assert 3290 <= await fake_redis.ttl(key) <= 3300


async def test_cached_tokens_are_encrypted(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        router.post(TOKENS_URL).mock(return_value=_token_response(clock, "ghs_supersecret"))
        await app.installation_token(INSTALLATION)
    raw = cast("str | None", await fake_redis.get(f"github:itoken:{INSTALLATION}:all"))
    assert raw is not None
    assert "ghs_supersecret" not in raw
    assert b"ghs_supersecret" not in base64.b64decode(raw)


async def test_short_lived_tokens_are_not_cached(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        route = router.post(TOKENS_URL).mock(return_value=_token_response(clock, minutes=4))
        await app.installation_token(INSTALLATION)
        await app.installation_token(INSTALLATION)
    assert route.call_count == 2


async def test_unreadable_cache_entries_are_replaced(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    await fake_redis.set(f"github:itoken:{INSTALLATION}:all", "bm90IGEgY2lwaGVydGV4dA==")
    with respx.mock() as router:
        router.post(TOKENS_URL).mock(return_value=_token_response(clock, "ghs_fresh"))
        assert (await app.installation_token(INSTALLATION)).token == "ghs_fresh"


async def test_scoped_tokens_ask_for_less_and_are_cached_apart(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        route = router.post(TOKENS_URL).mock(
            side_effect=[_token_response(clock, "ghs_scoped"), _token_response(clock, "ghs_all")]
        )
        scoped = await app.installation_token(
            INSTALLATION, repository_ids=[7, 3], permissions={"contents": "read"}
        )
        unscoped = await app.installation_token(INSTALLATION)
        again = await app.installation_token(
            INSTALLATION, repository_ids=[3, 7], permissions={"contents": "read"}
        )
    assert (scoped.token, unscoped.token, again.token) == ("ghs_scoped", "ghs_all", "ghs_scoped")
    assert json.loads(cast("list[respx.models.Call]", route.calls)[0].request.content) == {
        "repository_ids": [7, 3],
        "permissions": {"contents": "read"},
    }
    with pytest.raises(ValueError, match="at most 500"):
        await app.installation_token(INSTALLATION, repository_ids=list(range(501)))


async def test_installation_credentials_are_scoped_for_the_etag_cache(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        router.post(TOKENS_URL).mock(return_value=_token_response(clock))
        credential = await app.installation_credential(INSTALLATION)
    assert credential.header == "Bearer ghs_one"
    assert credential.scope == f"installation:{INSTALLATION}:all"


async def test_forget_tokens_drops_every_cached_scope(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        route = router.post(TOKENS_URL).mock(return_value=_token_response(clock))
        await app.installation_token(INSTALLATION)
        await app.installation_token(INSTALLATION, repository_ids=[1])
        await app.forget_tokens(INSTALLATION)
        assert await fake_redis.exists(f"github:itoken:{INSTALLATION}:all") == 0
        assert await fake_redis.exists(f"github:itoken:{INSTALLATION}:keys") == 0
        await app.installation_token(INSTALLATION)
    assert route.call_count == 3


async def test_malformed_token_response_is_an_error(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        router.post(TOKENS_URL).mock(return_value=httpx.Response(201, json={"nope": True}))
        with pytest.raises(GitHubError, match="unexpected access token response"):
            await app.installation_token(INSTALLATION)


async def test_get_installation_checks_the_id(
    fake_redis: FakeAsyncRedis, http: httpx.AsyncClient, clock: Clock
) -> None:
    app = _app(fake_redis, http, clock)
    with respx.mock() as router:
        router.get(f"{API}/app/installations/{INSTALLATION}").mock(
            return_value=httpx.Response(200, json={"id": 1})
        )
        with pytest.raises(GitHubError):
            await app.get_installation(INSTALLATION)
