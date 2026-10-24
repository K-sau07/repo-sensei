"""The Voyage client against a mocked API: batches, retries, splitting, rate limits and the usage
each request reports."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
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
async def test_other_bad_requests_are_not_split() -> None:
    # A 400 that mentions tokens, but not Voyage's batch limit, is a plain error.
    route = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"detail": "invalid token in input_type"})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageError, match="400: invalid token"):
            await _client(http, Sleeps()).embed(["a", "b"], InputType.DOCUMENT)
    assert route.call_count == 1


def _billed(data: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": data, "usage": {"total_tokens": 7}}


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(200, text="<html>oops</html>"), "isn't JSON"),
        (httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2, 3, 4]}]}), "no usage"),
        (httpx.Response(200, json=_billed([])), "expected 1 embeddings"),
        (httpx.Response(200, json=_billed([{"index": 0, "embedding": [1.0, 2.0]}])), "2 floats"),
        (httpx.Response(200, json=_billed([{"index": 0, "embedding": ["1", 0, 0, 0]}])), "number"),
        (httpx.Response(200, json=_billed([{"index": 0, "embedding": [True, 0, 0, 0]}])), "number"),
        (httpx.Response(200, json=_billed([{"index": 0, "embedding": [1e6, 0, 0, 0]}])), "range"),
        (
            httpx.Response(
                200,
                content=b'{"data": [{"index": 0, "embedding": [NaN, 0, 0, 0]}],'
                b' "usage": {"total_tokens": 7}}',
            ),
            "range",
        ),
        (httpx.Response(200, json=_billed([{"index": 3, "embedding": [0, 0, 0, 0]}])), "indexes"),
    ],
)
@respx.mock
async def test_a_bad_answer_is_a_voyage_error(answer: httpx.Response, error: str) -> None:
    respx.post(URL).mock(return_value=answer)
    usages, sink = _collect()
    async with httpx.AsyncClient() as http:
        with pytest.raises(VoyageError, match=error):
            await _client(http, Sleeps()).embed(["a"], InputType.DOCUMENT, on_usage=sink)
    # An answer that says what it billed is reported, even when it is then rejected.
    billed = json.loads(answer.content).get("usage") if b"usage" in answer.content else None
    assert [u.tokens for u in usages] == ([7] if billed else [])


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
    # What was billed before the failure is still reported.
    usages, sink = _collect()
    async with httpx.AsyncClient() as http:
        client = _client(http, Sleeps(), max_batch_texts=1, max_concurrency=1)
        with pytest.raises(VoyageError, match="403: forbidden"):
            await client.embed(["ok", "ok2", "bad"], InputType.DOCUMENT, on_usage=sink)
    assert [(u.texts, u.tokens) for u in usages] == [(1, 2), (1, 3)]


async def test_rate_limiter_paces_requests_and_tokens() -> None:
    now = [0.0]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(round(seconds, 3))
        now[0] += seconds

    limiter = RateLimiter(2, 600, clock=lambda: now[0], sleep=sleep)
    await limiter.acquire(100)
    now[0] += 10
    await limiter.acquire(100)
    assert waits == []
    await limiter.acquire(100)  # requests run out: the first leaves the window at 60 s
    assert waits == [50.0]
    waits.clear()
    now[0] += 200  # an empty window
    await limiter.acquire(600)
    await limiter.acquire(300)  # tokens run out: wait for the 600 to leave the window
    assert waits == [60.0]
    waits.clear()
    now[0] += 200
    await limiter.acquire(5000)  # more than a minute's tokens: an empty window, then it goes
    assert waits == []


async def test_no_minute_ever_holds_more_than_the_limits() -> None:
    rng = random.Random(7)  # noqa: S311 - a seeded test sequence, not a secret
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    rpm, tpm = 5, 1_000
    limiter = RateLimiter(rpm, tpm, clock=lambda: now[0], sleep=sleep)
    sent: list[tuple[float, int]] = []
    for _ in range(300):
        tokens = rng.randint(1, 400)
        await limiter.acquire(tokens)
        sent.append((now[0], tokens))
        now[0] += rng.choice([0.0, 0.0, 0.5, 3.0, 20.0])
    assert now[0] > 600  # it had to wait: the test exercised the limits
    for end, _ in sent:
        window = [t for when, t in sent if end - 60 < when <= end]
        assert len(window) <= rpm
        assert sum(window) <= tpm


@respx.mock
async def test_retries_count_against_the_rate_limit() -> None:
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(503, json={"detail": "busy"}), _ok(["x"])]
    )
    sleeps = Sleeps()
    async with httpx.AsyncClient() as http:
        await _client(http, sleeps, requests_per_minute=1).embed(["x"], InputType.DOCUMENT)
    assert route.call_count == 2
    # The backoff (1 s), then the rest of the minute the first attempt used.
    assert sleeps.calls[0] == 1.0
    assert round(sum(sleeps.calls), 3) == 60.0


@respx.mock
async def test_retry_after_may_be_an_http_date() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    respx.post(URL).mock(
        side_effect=[httpx.Response(429, headers={"retry-after": when}), _ok(["x"])]
    )
    sleeps = Sleeps()
    async with httpx.AsyncClient() as http:
        await _client(http, sleeps).embed(["x"], InputType.DOCUMENT)
    assert 25 <= sleeps.calls[0] <= 30
    respx.post(URL).mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "soon"}), _ok(["x"])]
    )
    sleeps = Sleeps()
    async with httpx.AsyncClient() as http:
        await _client(http, sleeps).embed(["x"], InputType.DOCUMENT)
    assert sleeps.calls == [1.0]  # unreadable: the usual backoff


def test_tests_never_take_the_developers_key() -> None:
    # A developer's .env (or shell) may hold a real key: tests must never spend it.
    assert VoyageSettings().api_key is None
    assert Settings().voyage.api_key is None
