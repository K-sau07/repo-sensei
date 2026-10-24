"""Voyage AI embeddings (``POST /v1/embeddings``), for code chunks and queries.

* **Batches** of at most ``max_batch_texts`` texts and ``max_batch_tokens`` tokens by our
  estimate (:func:`estimate_tokens`, on the high side), and never more than a minute's tokens
  (an account without a payment method gets 10K a minute; a bigger request is refused).
  Voyage counts with its own tokenizer, so a batch it still finds too big (a 400 that names the
  token limit) is split in two and sent again, down to single texts.
* **Concurrency**: at most ``max_concurrency`` requests at once; the first to fail cancels the
  rest and its error is raised.
* **Rate limits**: requests and tokens per minute are spent from buckets that refill
  continuously (:class:`RateLimiter`), so a big index paces itself instead of meeting 429s.
* **Retries**: network errors, 429 and 5xx, with exponential backoff and jitter, or after the
  ``Retry-After`` the answer gives (one longer than ``max_retry_wait_seconds`` gives up).
* **Cost**: each request's billed tokens (``usage.total_tokens``) go to ``on_usage`` as it
  finishes, one call at a time.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from sensei_core.config import VoyageSettings
from sensei_engine.embed.provider import EmbeddingUsage, InputType, UsageSink

CHARS_PER_TOKEN = 3  # code runs 3-4 characters a token; the lower bound keeps batches small
BACKOFF_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class VoyageError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Voyage {status}: {message}")
        self.status = status
        self.message = message


class VoyageNotConfigured(VoyageError):
    def __init__(self) -> None:
        super().__init__(0, "no API key (SENSEI_VOYAGE_API_KEY)")


class VoyageUnreachable(VoyageError):
    """No answer (network failure or timeout), after retries."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(503, f"unreachable ({type(cause).__name__})")


class VoyageRateLimited(VoyageError):
    """Still rate limited after the retries, or told to wait longer than we would."""

    def __init__(self, retry_after: float | None) -> None:
        wait = f"retry after {retry_after:.0f} s" if retry_after is not None else "retries spent"
        super().__init__(429, f"rate limited ({wait})")
        self.retry_after = retry_after


def estimate_tokens(text: str) -> int:
    """A token count on the high side for ``text`` (Voyage's tokenizer isn't public)."""
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def plan_batches(estimates: Sequence[int], *, max_texts: int, max_tokens: int) -> list[range]:
    """Consecutive index ranges, each at most ``max_texts`` long and ``max_tokens`` in total (a
    single text over the budget gets a batch of its own)."""
    batches: list[range] = []
    start, tokens = 0, 0
    for index, estimate in enumerate(estimates):
        full = index - start >= max_texts or tokens + estimate > max_tokens
        if index > start and full:
            batches.append(range(start, index))
            start, tokens = index, 0
        tokens += estimate
    if start < len(estimates):
        batches.append(range(start, len(estimates)))
    return batches


class RateLimiter:
    """Requests and tokens per minute as two buckets that refill continuously; :meth:`acquire`
    waits until both hold enough. Waiters are served in order."""

    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._request_rate = requests_per_minute / 60
        self._token_rate = tokens_per_minute / 60
        self._request_capacity = float(requests_per_minute)
        self._token_capacity = float(tokens_per_minute)
        self._requests = self._request_capacity
        self._tokens = self._token_capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed, self._updated = now - self._updated, now
        self._requests = min(self._request_capacity, self._requests + elapsed * self._request_rate)
        self._tokens = min(self._token_capacity, self._tokens + elapsed * self._token_rate)

    async def acquire(self, tokens: int) -> None:
        # A request bigger than a minute's tokens waits for a full bucket, then goes.
        needed = min(float(tokens), self._token_capacity)
        async with self._lock:
            while True:
                self._refill()
                if self._requests >= 1 and self._tokens >= needed:
                    self._requests -= 1
                    self._tokens -= needed
                    return
                wait = max(
                    (1 - self._requests) / self._request_rate,
                    (needed - self._tokens) / self._token_rate,
                )
                await self._sleep(wait)


@dataclass(slots=True)
class _Job:
    """One :meth:`VoyageClient.embed` call: its texts, and the vectors as batches fill them."""

    texts: Sequence[str]
    estimates: list[int]
    input_type: InputType
    on_usage: UsageSink | None
    vectors: list[list[float] | None] = field(default_factory=list[list[float] | None])
    report: asyncio.Lock = field(default_factory=asyncio.Lock)  # one on_usage at a time

    def __post_init__(self) -> None:
        self.vectors = [None] * len(self.texts)


class VoyageClient:
    """An :class:`~sensei_engine.embed.provider.EmbeddingProvider` for Voyage's API."""

    def __init__(
        self,
        settings: VoyageSettings,
        http: httpx.AsyncClient,
        *,
        limiter: RateLimiter | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._http = http
        self._sleep = sleep
        self._jitter = jitter
        self._limiter = limiter or RateLimiter(
            settings.requests_per_minute, settings.tokens_per_minute, sleep=sleep
        )

    @property
    def provider(self) -> str:
        return "voyage"

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def dims(self) -> int:
        return self._settings.dims

    async def embed(
        self,
        texts: Sequence[str],
        input_type: InputType,
        *,
        on_usage: UsageSink | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        if self._settings.api_key is None:
            raise VoyageNotConfigured
        job = _Job(texts, [estimate_tokens(text) for text in texts], input_type, on_usage)
        batches = plan_batches(
            job.estimates,
            max_texts=self._settings.max_batch_texts,
            # A request over a minute's tokens would be refused however long it waited.
            max_tokens=min(self._settings.max_batch_tokens, self._settings.tokens_per_minute),
        )
        gate = asyncio.Semaphore(self._settings.max_concurrency)

        async def run(batch: range) -> None:
            async with gate:
                await self._embed_range(job, batch)

        try:
            async with asyncio.TaskGroup() as group:
                for batch in batches:
                    group.create_task(run(batch))
        except ExceptionGroup as failed:  # the first failure; it cancelled the other batches
            raise failed.exceptions[0] from None
        done = [vector for vector in job.vectors if vector is not None]
        if len(done) != len(texts):  # pragma: no cover - every batch fills its range or raises
            raise VoyageError(502, "an embedding is missing")
        return done

    async def _embed_range(self, job: _Job, batch: range) -> None:
        """Embed ``job.texts[batch]``; a batch too big for the API is halved."""
        try:
            found, tokens = await self._request(job, batch)
        except VoyageError as error:
            if not (_too_many_tokens(error) and len(batch) > 1):
                raise
            middle = batch.start + len(batch) // 2
            for half in (range(batch.start, middle), range(middle, batch.stop)):
                await self._embed_range(job, half)
            return
        for index, vector in zip(batch, found, strict=True):
            job.vectors[index] = vector
        if job.on_usage is not None:
            async with job.report:
                await job.on_usage(EmbeddingUsage(self.provider, self.model, len(batch), tokens))

    async def _request(self, job: _Job, indexes: range) -> tuple[list[list[float]], int]:
        """One request for ``job.texts[indexes]``, retried; the vectors in order and the tokens
        billed."""
        batch = [job.texts[i] for i in indexes]
        key = self._settings.api_key
        if key is None:  # pragma: no cover - embed checked it
            raise VoyageNotConfigured
        body = {
            "input": batch,
            "model": self.model,
            "input_type": job.input_type.value,
            "output_dimension": self.dims,
            "output_dtype": "float",
            "truncation": True,
        }
        headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
        url = f"{self._settings.api_url.rstrip('/')}/embeddings"
        await self._limiter.acquire(sum(job.estimates[i] for i in indexes))
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._http.post(
                    url, json=body, headers=headers, timeout=self._settings.timeout_seconds
                )
            except httpx.TransportError as exc:
                if attempt > self._settings.max_retries:
                    raise VoyageUnreachable(exc) from exc
                await self._sleep(self._backoff(attempt))
                continue
            status = response.status_code
            if status == 429 or status >= 500:
                retry_after = _retry_after(response)
                if attempt > self._settings.max_retries:
                    if status == 429:
                        raise VoyageRateLimited(retry_after)
                    raise VoyageError(status, _message(response))
                if retry_after is not None and retry_after > self._settings.max_retry_wait_seconds:
                    raise VoyageRateLimited(retry_after)
                await self._sleep(
                    retry_after if retry_after is not None else self._backoff(attempt)
                )
                continue
            if status >= 400:
                raise VoyageError(status, _message(response))
            return self._parse(response.json(), len(batch))

    def _parse(self, payload: Any, expected: int) -> tuple[list[list[float]], int]:
        body = _object(payload)
        data = body.get("data")
        if not isinstance(data, list) or len(cast("list[Any]", data)) != expected:
            raise VoyageError(502, f"expected {expected} embeddings in the answer")
        by_index: dict[int, list[float]] = {}
        for raw in cast("list[Any]", data):
            item = _object(raw)
            embedding, index = item.get("embedding"), item.get("index")
            if not isinstance(embedding, list) or not isinstance(index, int):
                raise VoyageError(502, "an embedding without its vector or index")
            values = cast("list[Any]", embedding)
            if len(values) != self.dims:
                raise VoyageError(502, f"an embedding of {len(values)} floats, not {self.dims}")
            by_index[index] = [float(value) for value in values]
        if sorted(by_index) != list(range(expected)):
            raise VoyageError(502, "the answer's indexes don't match the request")
        tokens = _object(body.get("usage")).get("total_tokens")
        return [by_index[i] for i in range(expected)], tokens if isinstance(tokens, int) else 0

    def _backoff(self, attempt: int) -> float:
        base = min(BACKOFF_CAP_SECONDS, BACKOFF_SECONDS * 2 ** (attempt - 1))
        return base * (0.5 + self._jitter() / 2)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _object(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300] or response.reason_phrase
    body = _object(payload)
    detail = body.get("detail") or body.get("error") or body.get("message")
    return str(detail)[:300] if detail else response.text[:300]


def _too_many_tokens(error: VoyageError) -> bool:
    """Voyage's answer to a batch over its token limit (a 400 naming the batch's tokens)."""
    return error.status == 400 and "token" in error.message.lower()
