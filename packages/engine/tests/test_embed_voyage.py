"""The Voyage client against a mocked API: batches, retries, splitting, rate limits and the usage
each request reports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
import respx
import respx.models
from pydantic import SecretStr

from sensei_core.config import Settings, VoyageSettings
from sensei_engine.embed.provider import EmbeddingUsage, InputType
from sensei_engine.embed.voyage import (
    RateLimiter,
    VoyageClient,
    VoyageError,
    VoyageNotConfigured,
    VoyageRateLimited,
    VoyageUnreachable,
    estimate_tokens,
    plan_batches,
)

URL = "https://api.voyageai.com/v1/embeddings"
DIMS = 4


class Sleeps:
    """A fake sleep that moves a fake clock on (the rate limiter reads it)."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.now = 0.0

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def _settings(**overrides: Any) -> VoyageSettings:
    values: dict[str, Any] = {"api_key": SecretStr("pa-test"), "dims": DIMS, **overrides}
    return VoyageSettings(**values)


def _vector(text: str) -> list[float]:
    """A vector that says which text it is for (its length, then fillers)."""
    return [float(len(text)), 0.5, 0.25, 0.125]


def _answer(request: httpx.Request, *, reverse: bool = False) -> httpx.Response:
    body = json.loads(request.content)
    data = [{"index": i, "embedding": _vector(text)} for i, text in enumerate(body["input"])]
    if reverse:  # the API may answer out of order: the index says where each goes
        data.reverse()
    tokens = sum(len(text) for text in body["input"])
    return httpx.Response(
        200, json={"data": data, "model": body["model"], "usage": {"total_tokens": tokens}}
    )


def _reversed(request: httpx.Request) -> httpx.Response:
    return _answer(request, reverse=True)


def _ok(texts: list[str]) -> httpx.Response:
    data = [{"index": i, "embedding": _vector(text)} for i, text in enumerate(texts)]
    return httpx.Response(200, json={"data": data, "usage": {"total_tokens": len(texts)}})


def _bodies(route: respx.Route) -> list[dict[str, Any]]:
    calls = cast("list[respx.models.Call]", route.calls)
    return [json.loads(call.request.content) for call in calls]


def _client(http: httpx.AsyncClient, sleeps: Sleeps, **overrides: Any) -> VoyageClient:
    settings = _settings(**overrides)
    limiter = RateLimiter(
        settings.requests_per_minute,
        settings.tokens_per_minute,
        clock=sleeps.clock,
        sleep=sleeps,
    )
    return VoyageClient(settings, http, limiter=limiter, sleep=sleeps, jitter=lambda: 1.0)


Usages = list[EmbeddingUsage]


def _collect() -> tuple[Usages, Callable[[EmbeddingUsage], Any]]:
    usages: Usages = []

    async def sink(usage: EmbeddingUsage) -> None:
        usages.append(usage)

    return usages, sink


def test_plan_batches_respects_count_and_tokens() -> None:
    assert plan_batches([1] * 7, max_texts=3, max_tokens=100) == [
        range(0, 3), range(3, 6), range(6, 7),
    ]  # fmt: skip
    assert plan_batches([40, 40, 40, 10], max_texts=10, max_tokens=100) == [
        range(0, 2), range(2, 4),
    ]  # fmt: skip
    # A text over the whole budget goes alone; the batch boundary is exact at the budget.
    assert plan_batches([150, 50, 50, 1], max_texts=10, max_tokens=100) == [
        range(0, 1), range(1, 3), range(3, 4),
    ]  # fmt: skip
    assert plan_batches([], max_texts=3, max_tokens=10) == []
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcdefg") == 3  # on the high side: 3 characters a token


@respx.mock
async def test_embeds_in_order_with_the_documented_request() -> None:
    route = respx.post(URL).mock(side_effect=_reversed)
    usages, sink = _collect()
    async with httpx.AsyncClient() as http:
        client = _client(http, Sleeps())
        vectors = await client.embed(["a", "bbb", "cc"], InputType.DOCUMENT, on_usage=sink)
    assert vectors == [_vector("a"), _vector("bbb"), _vector("cc")]
    [body] = _bodies(route)
    assert body == {
        "input": ["a", "bbb", "cc"],
        "model": "voyage-code-4",
        "input_type": "document",
        "output_dimension": DIMS,
        "output_dtype": "float",
        "truncation": True,
    }
    request = cast("list[respx.models.Call]", route.calls)[0].request
    assert request.headers["authorization"] == "Bearer pa-test"
    assert usages == [EmbeddingUsage("voyage", "voyage-code-4", texts=3, tokens=6)]
    assert (client.provider, client.model, client.dims) == ("voyage", "voyage-code-4", DIMS)


@respx.mock
async def test_batches_follow_the_limits_and_each_reports_its_usage() -> None:
    route = respx.post(URL).mock(side_effect=_answer)
    usages, sink = _collect()
    texts = [f"text {i}" for i in range(7)]  # 6 characters: 2 tokens each by our estimate
    async with httpx.AsyncClient() as http:
        client = _client(http, Sleeps(), max_batch_texts=3, max_concurrency=1)
        vectors = await client.embed(texts, InputType.QUERY, on_usage=sink)
        assert vectors == [_vector(text) for text in texts]
        assert [len(body["input"]) for body in _bodies(route)] == [3, 3, 1]
        assert {body["input_type"] for body in _bodies(route)} == {"query"}
        assert [usage.texts for usage in usages] == [3, 3, 1]
        assert sum(usage.tokens for usage in usages) == 6 * 7  # what the API billed
        route.reset()
        client = _client(http, Sleeps(), max_batch_tokens=4, max_concurrency=1)
        await client.embed(texts[:5], InputType.DOCUMENT)
        assert [len(body["input"]) for body in _bodies(route)] == [2, 2, 1]
        route.reset()
        # A minute's tokens cap a batch too: 4 a minute, so 2 texts of 2 tokens each.
        client = _client(http, Sleeps(), tokens_per_minute=4, max_concurrency=1)
        await client.embed(texts[:3], InputType.DOCUMENT)
        assert [len(body["input"]) for body in _bodies(route)] == [2, 1]
    assert await client.embed([], InputType.DOCUMENT) == []


@respx.mock
async def test_retries_server_errors_and_rate_limits() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(503, json={"detail": "busy"}),
            httpx.Response(429, headers={"retry-after": "2"}, json={"detail": "slow down"}),
            httpx.ConnectError("reset"),
            _ok(["x"]),
        ]
    )
    sleeps = Sleeps()
    async with httpx.AsyncClient() as http:
        vectors = await _client(http, sleeps).embed(["x"], InputType.DOCUMENT)
    assert vectors == [_vector("x")]
    assert route.call_count == 4
    # Backoff doubles (jitter at its top, so the full step), Retry-After is obeyed as given.
    assert sleeps.calls == [1.0, 2.0, 4.0]


@respx.mock
async def test_gives_up_after_the_retries() -> None:
    respx.post(URL).mock(return_value=httpx.Response(502, json={"detail": "bad gateway"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageError, match="Voyage 502: bad gateway"):
            await _client(http, Sleeps(), max_retries=2).embed(["x"], InputType.DOCUMENT)
    respx.post(URL).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageRateLimited):
            await _client(http, Sleeps(), max_retries=1).embed(["x"], InputType.DOCUMENT)
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageUnreachable):
            await _client(http, Sleeps(), max_retries=1).embed(["x"], InputType.DOCUMENT)


@respx.mock
async def test_a_long_retry_after_gives_up_at_once() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(429, headers={"retry-after": "600"}))
    sleeps = Sleeps()
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageRateLimited) as raised:
            await _client(http, sleeps).embed(["x"], InputType.DOCUMENT)
    assert raised.value.retry_after == 600
    assert (route.call_count, sleeps.calls) == (1, [])


@respx.mock
async def test_client_errors_are_not_retried() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(401, json={"detail": "bad key"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageError, match="401: bad key"):
            await _client(http, Sleeps()).embed(["x"], InputType.DOCUMENT)
    assert route.call_count == 1


@respx.mock
async def test_a_batch_over_the_api_token_limit_is_split_in_two() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if len(json.loads(request.content)["input"]) > 2:
            detail = "The max allowed tokens per submitted batch is 120000. Your batch has 130000."
            return httpx.Response(400, json={"detail": detail})
        return _answer(request)

    route = respx.post(URL).mock(side_effect=answer)
    usages, sink = _collect()
    texts = ["a", "b", "c", "d", "e"]
    async with httpx.AsyncClient() as http:
        vectors = await _client(http, Sleeps()).embed(texts, InputType.DOCUMENT, on_usage=sink)
    assert vectors == [_vector(text) for text in texts]
    assert [len(body["input"]) for body in _bodies(route)] == [5, 2, 3, 1, 2]
    assert [usage.texts for usage in usages] == [2, 1, 2]  # only requests that were answered


@respx.mock
async def test_other_bad_requests_and_bad_answers_raise() -> None:
    respx.post(URL).mock(return_value=httpx.Response(400, json={"detail": "bad input_type"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageError, match="400: bad input_type"):
            await _client(http, Sleeps()).embed(["a", "b"], InputType.DOCUMENT)
    wrong = {"data": [{"index": 0, "embedding": [1.0, 2.0]}], "usage": {"total_tokens": 1}}
    respx.post(URL).mock(return_value=httpx.Response(200, json=wrong))
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageError, match="2 floats, not 4"):
            await _client(http, Sleeps()).embed(["a"], InputType.DOCUMENT)
    respx.post(URL).mock(return_value=httpx.Response(200, json={"data": []}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageError, match="expected 1 embeddings"):
            await _client(http, Sleeps()).embed(["a"], InputType.DOCUMENT)


async def test_without_a_key_nothing_is_sent() -> None:
    async with httpx.AsyncClient() as http:
        client = VoyageClient(VoyageSettings(api_key=None), http)
        with pytest.raises(VoyageNotConfigured):
            await client.embed(["a"], InputType.DOCUMENT)


@respx.mock
async def test_usage_is_reported_one_request_at_a_time() -> None:
    respx.post(URL).mock(side_effect=_answer)
    active, overlapped, reported = 0, False, 0

    async def sink(usage: EmbeddingUsage) -> None:
        nonlocal active, overlapped, reported
        active += 1
        overlapped = overlapped or active > 1
        await asyncio.sleep(0.01)  # a session write, say
        active -= 1
        reported += usage.texts

    async with httpx.AsyncClient() as http:
        client = _client(http, Sleeps(), max_batch_texts=1, max_concurrency=4)
        await client.embed([str(i) for i in range(8)], InputType.DOCUMENT, on_usage=sink)
    assert (reported, overlapped) == (8, False)


@respx.mock
async def test_one_failed_batch_cancels_the_others() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["input"] == ["bad"]:
            return httpx.Response(403, json={"detail": "forbidden"})
        return _answer(request)

    respx.post(URL).mock(side_effect=answer)
    async with httpx.AsyncClient() as http:
        client = _client(http, Sleeps(), max_batch_texts=1, max_concurrency=2)
        with pytest.raises(VoyageError, match="403: forbidden"):
            await client.embed(["ok", "bad", "ok"], InputType.DOCUMENT)


async def test_rate_limiter_paces_requests_and_tokens() -> None:
    now = [0.0]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(round(seconds, 6))
        now[0] += seconds

    limiter = RateLimiter(2, 600, clock=lambda: now[0], sleep=sleep)
    await limiter.acquire(100)
    await limiter.acquire(100)
    assert waits == []  # a full minute's budget to start with
    await limiter.acquire(100)  # requests run out: one refills every 30 s
    assert waits == [30.0]
    waits.clear()
    now[0] += 60  # both buckets full again
    await limiter.acquire(600)
    await limiter.acquire(300)  # tokens run out: 10 a second refill
    assert waits == [30.0]
    waits.clear()
    now[0] += 120
    await limiter.acquire(5000)  # more than a minute's tokens: waits for a full bucket, then goes
    assert waits == []


def test_tests_never_take_the_developers_key() -> None:
    # A developer's .env (or shell) may hold a real key: tests must never spend it.
    assert VoyageSettings().api_key is None
    assert Settings().voyage.api_key is None
