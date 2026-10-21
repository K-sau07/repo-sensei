"""GitHub REST client: retries, rate limits, conditional requests and pagination (mocked GitHub)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
import respx
import respx.models
from fakeredis import FakeAsyncRedis

from sensei_core.config import AuthSettings, GitHubSettings
from sensei_core.github.client import (
    Credential,
    GitHubClient,
    GitHubError,
    GitHubUnreachable,
    RateLimited,
)
from sensei_core.github.etags import RedisETagCache
from sensei_core.security.crypto import TokenCipher

API = "https://api.github.com"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
ALICE = Credential("Bearer alice", "user:alice")
BOB = Credential("Bearer bob", "user:bob")


def _requests(route: respx.Route) -> list[httpx.Request]:
    calls = cast("list[respx.models.Call]", route.calls)
    return [call.request for call in calls]


class MemoryETags:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[str, Any]] = {}

    async def get(self, key: str) -> tuple[str, Any] | None:
        return self.entries.get(key)

    async def set(self, key: str, etag: str, body: Any) -> None:
        self.entries[key] = (etag, body)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def _client(
    http: httpx.AsyncClient, sleeps: Sleeps, *, etags: Any = None, **settings: Any
) -> GitHubClient:
    return GitHubClient(
        GitHubSettings(**settings), http, etags=etags, sleep=sleeps, now=lambda: NOW
    )


async def test_sends_github_headers(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        route = router.get(f"{API}/user").mock(return_value=httpx.Response(200, json={"id": 1}))
        assert await _client(http, sleeps).get_json("/user", ALICE) == {"id": 1}
    sent = route.calls.last.request.headers
    assert sent["authorization"] == "Bearer alice"
    assert sent["accept"] == "application/vnd.github+json"
    assert sent["x-github-api-version"] == "2022-11-28"


async def test_retries_server_errors_with_backoff(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        route = router.get(f"{API}/user").mock(
            side_effect=[httpx.Response(502), httpx.Response(503), httpx.Response(200, json={})]
        )
        await _client(http, sleeps).get_json("/user", ALICE)
    assert route.call_count == 3
    assert len(sleeps.calls) == 2
    assert 0.25 <= sleeps.calls[0] <= 0.75  # 0.5s with jitter
    assert 0.5 <= sleeps.calls[1] <= 1.5


async def test_gives_up_after_max_retries(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        route = router.get(f"{API}/user").mock(return_value=httpx.Response(500))
        with pytest.raises(GitHubError) as exc:
            await _client(http, sleeps, max_retries=2).get_json("/user", ALICE)
    assert exc.value.status == 500
    assert route.call_count == 3


async def test_retries_network_errors(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        router.get(f"{API}/user").mock(
            side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"ok": True})]
        )
        assert await _client(http, sleeps).get_json("/user", ALICE) == {"ok": True}
        router.get(f"{API}/orgs").mock(side_effect=httpx.ConnectError("down"))
        # After the retries it is a GitHubError like any other: callers handle one kind.
        with pytest.raises(GitHubUnreachable) as exc:
            await _client(http, sleeps, max_retries=1).get_json("/orgs", ALICE)
        assert exc.value.status == 503
        assert isinstance(exc.value.__cause__, httpx.ConnectError)


async def test_client_errors_are_not_retried(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        route = router.get(f"{API}/repos/x/y").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        with pytest.raises(GitHubError, match="Not Found") as exc:
            await _client(http, sleeps).get_json("/repos/x/y", ALICE)
    assert (exc.value.status, route.call_count, sleeps.calls) == (404, 1, [])


async def test_permission_403_is_not_a_rate_limit(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        router.get(f"{API}/x").mock(
            return_value=httpx.Response(
                403,
                json={"message": "Resource not accessible"},
                headers={"x-ratelimit-remaining": "4999"},
            )
        )
        with pytest.raises(GitHubError) as exc:
            await _client(http, sleeps).get_json("/x", ALICE)
    assert not isinstance(exc.value, RateLimited)
    assert sleeps.calls == []


async def test_waits_for_a_primary_rate_limit_that_resets_soon(sleeps: Sleeps) -> None:
    reset = int(NOW.timestamp()) + 10
    limited = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
    )
    async with httpx.AsyncClient() as http, respx.mock() as router:
        router.get(f"{API}/x").mock(side_effect=[limited, httpx.Response(200, json=[])])
        assert await _client(http, sleeps).get_json("/x", ALICE) == []
    assert sleeps.calls == [11.0]  # until the reset, plus a second of slack


async def test_waits_for_a_secondary_rate_limit(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        router.get(f"{API}/x").mock(
            side_effect=[httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200)]
        )
        await _client(http, sleeps).request("GET", "/x", ALICE)
    assert sleeps.calls == [3.0]


async def test_repeated_rate_limits_give_up(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        route = router.get(f"{API}/x").mock(
            return_value=httpx.Response(429, headers={"retry-after": "2"})
        )
        with pytest.raises(RateLimited):
            await _client(http, sleeps, max_retries=2).get_json("/x", ALICE)
    assert (route.call_count, sleeps.calls) == (3, [2.0, 2.0])


async def test_retry_after_can_be_a_date(sleeps: Sleeps) -> None:
    async with httpx.AsyncClient() as http, respx.mock() as router:
        router.get(f"{API}/x").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "Mon, 19 Oct 2026 12:00:05 GMT"}),
                httpx.Response(200, json={}),
            ]
        )
        await _client(http, sleeps).get_json("/x", ALICE)
    assert sleeps.calls == [5.0]


async def test_raises_rate_limited_when_the_reset_is_far(sleeps: Sleeps) -> None:
    reset = int(NOW.timestamp()) + 600
    async with httpx.AsyncClient() as http, respx.mock() as router:
        router.get(f"{API}/x").mock(
            return_value=httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
            )
        )
        with pytest.raises(RateLimited) as exc:
            await _client(http, sleeps).get_json("/x", ALICE)
    assert exc.value.reset_at == datetime.fromtimestamp(reset + 1, UTC)
    assert sleeps.calls == []


async def test_conditional_get_serves_cached_body_on_304(sleeps: Sleeps) -> None:
    etags = MemoryETags()
    async with httpx.AsyncClient() as http, respx.mock() as router:
        route = router.get(f"{API}/repos/a/b").mock(
            side_effect=[
                httpx.Response(200, json={"name": "b"}, headers={"etag": 'W/"v1"'}),
                httpx.Response(304),
            ]
        )
        client = _client(http, sleeps, etags=etags)
        first = await client.get_json("/repos/a/b", ALICE)
        second = await client.get_json("/repos/a/b", ALICE)
    assert first == second == {"name": "b"}
    assert "if-none-match" not in _requests(route)[0].headers
    assert _requests(route)[1].headers["if-none-match"] == 'W/"v1"'


async def test_etag_cache_is_per_credential(sleeps: Sleeps) -> None:
    """Bob must never be served what GitHub showed Alice."""
    etags = MemoryETags()
    async with httpx.AsyncClient() as http, respx.mock() as router:
        route = router.get(f"{API}/user/installations").mock(
            return_value=httpx.Response(200, json={"installations": []}, headers={"etag": '"x"'})
        )
        client = _client(http, sleeps, etags=etags)
        await client.get_json("/user/installations", ALICE)
        await client.get_json("/user/installations", BOB)
    assert "if-none-match" not in _requests(route)[1].headers
    assert len(etags.entries) == 2


async def test_paginate_follows_next_links(sleeps: Sleeps) -> None:
    page2 = f"{API}/user/installations?per_page=2&page=2"
    async with httpx.AsyncClient() as http, respx.mock() as router:
        router.get(page2).mock(
            return_value=httpx.Response(200, json={"installations": [{"id": 3}]})
        )
        router.get(f"{API}/user/installations", params={"per_page": "2"}).mock(
            return_value=httpx.Response(
                200,
                json={"installations": [{"id": 1}, {"id": 2}]},
                headers={"link": f'<{page2}>; rel="next", <{page2}>; rel="last"'},
            )
        )
        client = _client(http, sleeps)
        items = [
            item
            async for item in client.paginate(
                "/user/installations", ALICE, item_key="installations", per_page=2
            )
        ]
    assert [item["id"] for item in items] == [1, 2, 3]


async def test_pagination_continues_past_a_cached_first_page(sleeps: Sleeps) -> None:
    """A 304 for page 1 must still lead to page 2 (the Link header is cached with the body)."""
    etags = MemoryETags()
    page2 = f"{API}/user/installations?per_page=2&page=2"
    async with httpx.AsyncClient() as http, respx.mock() as router:
        router.get(page2).mock(
            return_value=httpx.Response(200, json={"installations": [{"id": 3}]})
        )
        router.get(f"{API}/user/installations", params={"per_page": "2"}).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"installations": [{"id": 1}, {"id": 2}]},
                    headers={"etag": '"p1"', "link": f'<{page2}>; rel="next"'},
                ),
                httpx.Response(304),
            ]
        )
        client = _client(http, sleeps, etags=etags)
        runs = [
            [
                item["id"]
                async for item in client.paginate(
                    "/user/installations", ALICE, item_key="installations", per_page=2
                )
            ]
            for _ in range(2)
        ]
    assert runs == [[1, 2, 3], [1, 2, 3]]


async def test_redis_etag_cache_encrypts_bodies() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    cipher = TokenCipher.from_settings(AuthSettings())
    cache = RedisETagCache(redis, cipher)
    await cache.set("k1", '"e1"', {"secret_file": "TOP-SECRET"})
    raw = await redis.get("github:etag:k1")
    assert raw is not None
    assert "TOP-SECRET" not in base64.b64decode(raw).decode(errors="replace")
    assert await cache.get("k1") == ('"e1"', {"secret_file": "TOP-SECRET"})
    assert 0 < await redis.ttl("github:etag:k1") <= 86_400
    # Moved to another key (or tampered with), the entry no longer decrypts and is dropped.
    await redis.set("github:etag:k2", raw)
    assert await cache.get("k2") is None
    assert await redis.exists("github:etag:k2") == 0
    await redis.aclose()
