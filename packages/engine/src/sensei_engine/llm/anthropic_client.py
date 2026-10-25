"""Claude through Anthropic's SDK: one request (:meth:`AnthropicClient.complete`), token counts,
and the Message Batches API (half price, results within 24 hours) for bulk work like summaries.

* **Models by tier** (:class:`Tier`), from settings: ``fast`` (Haiku) for bulk work, ``standard``
  (Sonnet), ``deep`` (Opus). Callers ask by :class:`Role` (router, summary, agent, escalate),
  mapped to a tier by :func:`tier_for`.
* **Effort** (``low`` … ``max``) sets how much the model thinks; the newest models think by
  default, and thinking counts toward ``max_tokens``.
* **Structured output**: a request with a JSON schema gets JSON matching it back
  (``output_config.format``), parsed into :attr:`LLMResult.data`.
* **Refusals**: a request a safety classifier declines is retried server-side on the model
  Anthropic recommends for that category (``fallbacks: "default"``; a model without fallbacks
  runs alone). One still declined raises :class:`LLMRefused`. Batches can't fall back.
* **Retries**: the SDK retries connection errors, 408, 409, 429 and 5xx with backoff; what is
  left raises :class:`LLMError` (:class:`LLMUnavailable` when there was no answer).
* **Cost**: each response's usage goes to ``on_usage``, once per model that ran (a fallback bills
  the declined attempt and the model that answered), and batch usage is marked as such (half
  price).
* **Nothing from the environment**: the key is passed explicitly, so the SDK reads no
  ``ANTHROPIC_API_KEY``, token or local profile; ``ANTHROPIC_CUSTOM_HEADERS`` (which the SDK would
  add to every request) is refused, and ``ANTHROPIC_LOG`` can't turn on the SDK's debug logs
  (they would print prompts, which hold customers' code).
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import re
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import anthropic
import httpx2
from anthropic.types import Message
from anthropic.types.beta import (
    BetaFallbackMessageIterationUsage,
    BetaMessage,
    BetaMessageIterationUsage,
)
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse
from anthropic.types.messages.batch_create_params import Request

from sensei_core.config import AnthropicSettings

PROVIDER = "anthropic"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CUSTOM_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # the Batches API's rule for request ids (whole)

# The SDK sets these from ANTHROPIC_LOG at import; its debug output includes request bodies.
for _name in ("anthropic", "httpx2"):
    logging.getLogger(_name).setLevel(logging.WARNING)

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Tier(enum.StrEnum):
    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"


class Role(enum.StrEnum):
    """What a call is for; each role runs on a tier (:func:`tier_for`)."""

    ROUTER = "router"  # query routing
    RERANK = "rerank"  # ordering search results
    RERANK_DEEP = "rerank_deep"  # ordering search results in deep mode
    SUMMARY = "summary"  # file and symbol summaries, rollups
    AGENT = "agent"  # the answering agent
    ESCALATE = "escalate"  # the hardest questions


ROLE_TIERS: Mapping[Role, Tier] = {
    Role.ROUTER: Tier.FAST,
    Role.RERANK: Tier.FAST,
    Role.RERANK_DEEP: Tier.STANDARD,
    Role.SUMMARY: Tier.FAST,
    Role.AGENT: Tier.STANDARD,
    Role.ESCALATE: Tier.DEEP,
}


def tier_for(role: Role) -> Tier:
    return ROLE_TIERS[role]


class LLMError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Anthropic {status}: {message}")
        self.status = status
        self.message = message

    @property
    def transient(self) -> bool:
        """Worth trying again later: no answer, rate limited, overloaded or a server error."""
        return self.status == 429 or self.status >= 500


class LLMNotConfigured(LLMError):
    def __init__(self) -> None:
        super().__init__(0, "no API key (SENSEI_ANTHROPIC_API_KEY)")


class LLMUnavailable(LLMError):
    """No answer (network failure or timeout), after the SDK's retries."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(503, f"unreachable ({type(cause).__name__})")


class LLMRefused(LLMError):
    """Declined by a safety classifier (after any fallback)."""

    def __init__(self, category: str | None) -> None:
        super().__init__(200, f"refused ({category or 'no category'})")
        self.category = category


class LLMTruncated(LLMError):
    """The answer hit ``max_tokens`` before it was complete."""

    def __init__(self, max_tokens: int | None) -> None:
        limit = f"max_tokens={max_tokens}" if max_tokens is not None else "max_tokens"
        super().__init__(200, f"answer cut off at {limit}")


class LLMBadOutput(LLMError):
    """The answer isn't the JSON the request asked for."""

    def __init__(self, detail: str) -> None:
        super().__init__(200, f"unusable answer: {detail}")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    prompt: str
    system: str | None = None
    tier: Tier = Tier.STANDARD
    max_tokens: int = 16_000
    effort: Effort | None = None
    # A JSON schema the answer must match (objects need "additionalProperties": false).
    schema: Mapping[str, Any] | None = None
    # Mark the system prompt for prompt caching (many requests share it; too short a prompt
    # silently isn't cached).
    cache_system: bool = False


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """One model's work on one response, as the provider billed it."""

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    batch: bool = False


LLMUsageSink = Callable[[LLMUsage], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    data: Any  # the parsed JSON when the request had a schema, else None
    model: str  # the model that answered (a fallback's, when one did)
    usage: tuple[LLMUsage, ...]
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class BatchState:
    id: str
    status: Literal["in_progress", "canceling", "ended"]
    succeeded: int
    errored: int
    canceled: int
    expired: int
    created_at: datetime
    request_count: int  # every request, whatever its state

    @classmethod
    def of(cls, batch: MessageBatch) -> BatchState:
        counts = batch.request_counts
        total = (
            counts.processing + counts.succeeded + counts.errored + counts.canceled + counts.expired
        )
        return cls(
            batch.id,
            batch.processing_status,
            counts.succeeded,
            counts.errored,
            counts.canceled,
            counts.expired,
            batch.created_at,
            total,
        )


@dataclass(frozen=True, slots=True)
class BatchItem:
    """One request's outcome. ``succeeded``: the model answered and ``usage`` was billed; the
    answer is ``result``, or ``error`` when it is unusable (refused, cut off, not JSON).
    ``errored``: the request failed (``error``); ``canceled`` and ``expired`` requests never ran
    and can be sent again."""

    custom_id: str
    outcome: Literal["succeeded", "errored", "canceled", "expired"]
    result: LLMResult | None = None
    error: LLMError | None = None
    usage: tuple[LLMUsage, ...] = ()


class AnthropicClient:
    def __init__(
        self, settings: AnthropicSettings, *, http_client: httpx2.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        self._http = http_client
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def configured(self) -> bool:
        return self._settings.configured

    def model_for(self, tier: Tier) -> str:
        match tier:
            case Tier.FAST:
                return self._settings.fast_model
            case Tier.STANDARD:
                return self._settings.standard_model
            case Tier.DEEP:
                return self._settings.deep_model

    async def complete(
        self, request: LLMRequest, *, on_usage: LLMUsageSink | None = None
    ) -> LLMResult:
        """One request. Raises :class:`LLMRefused`, :class:`LLMTruncated`,
        :class:`LLMBadOutput` or :class:`LLMError`; usage is reported before any of the first
        three (the work was done and billed)."""
        fallback = self._settings.refusal_fallback
        with _sdk_errors():
            message = await self._sdk().beta.messages.create(
                model=self.model_for(request.tier),
                max_tokens=request.max_tokens,
                messages=[{"role": "user", "content": request.prompt}],
                system=_system(request),
                output_config=_output_config(request),
                betas=[FALLBACK_BETA] if fallback else anthropic.omit,
                fallbacks="default" if fallback else anthropic.omit,
            )
        usage = _beta_usage(message, self.model_for(request.tier))
        if on_usage is not None:
            for item in usage:
                await on_usage(item)
        category = message.stop_details.category if message.stop_details is not None else None
        text = _text(block.text for block in message.content if block.type == "text")
        return _result(
            _Answer(text, message.model, message.stop_reason, category, usage),
            json_output=request.schema is not None,
            max_tokens=request.max_tokens,
            # Public despite the underscore (the SDK's documented request id).
            request_id=message._request_id,  # pyright: ignore[reportPrivateUsage]
        )

    async def complete_within(
        self,
        request: LLMRequest,
        *,
        timeout_seconds: float,
        on_usage: LLMUsageSink | None = None,
    ) -> LLMResult:
        """:meth:`complete`, given up after ``timeout_seconds`` (``TimeoutError``). A call given
        up on, or cancelled from outside (a client that went away), may still be billed, and its
        answer never says how much: ``on_usage`` gets the most it can cost (:func:`ceiling_usage`)
        instead."""
        reported = False

        async def report(usage: LLMUsage) -> None:
            nonlocal reported
            reported = True
            if on_usage is not None:
                await on_usage(usage)

        try:
            async with asyncio.timeout(timeout_seconds):
                return await self.complete(request, on_usage=report)
        except (TimeoutError, asyncio.CancelledError):
            if on_usage is not None and not reported:
                await on_usage(ceiling_usage(request, self.model_for(request.tier)))
            raise

    async def count_tokens(self, request: LLMRequest) -> int:
        """The input tokens ``request`` would use (free; rate limited separately)."""
        with _sdk_errors():
            counted = await self._sdk().messages.count_tokens(
                model=self.model_for(request.tier),
                messages=[{"role": "user", "content": request.prompt}],
                system=_system(request),
                # The schema is part of the prompt the model reads: count it too.
                output_config=_output_config(request),
            )
        return counted.input_tokens

    async def submit_batch(self, requests: Sequence[tuple[str, LLMRequest]]) -> BatchState:
        """Send ``(custom id, request)`` pairs as one batch (ids are unique, 1-64 letters,
        digits, ``_`` or ``-``)."""
        ids = [custom_id for custom_id, _ in requests]
        if not ids:
            msg = "a batch needs at least one request"
            raise ValueError(msg)
        if len(set(ids)) != len(ids) or not all(CUSTOM_ID.fullmatch(i) for i in ids):
            msg = "batch request ids must be unique and match [A-Za-z0-9_-]{1,64}"
            raise ValueError(msg)
        batch_requests = [
            Request(custom_id=custom_id, params=self._batch_params(request))
            for custom_id, request in requests
        ]
        with _sdk_errors():
            batch = await self._sdk().messages.batches.create(requests=batch_requests)
        return BatchState.of(batch)

    async def batch_state(self, batch_id: str) -> BatchState:
        with _sdk_errors():
            batch = await self._sdk().messages.batches.retrieve(batch_id)
        return BatchState.of(batch)

    async def recent_batches(self, since: datetime) -> list[BatchState]:
        """The account's batches created at or after ``since``, newest first."""
        found: list[BatchState] = []
        with _sdk_errors():
            async for batch in self._sdk().messages.batches.list(limit=100):
                if batch.created_at < since:
                    break  # the list is newest first
                found.append(BatchState.of(batch))
        return found

    async def batch_results(
        self, batch_id: str, *, json_output: bool = False
    ) -> AsyncGenerator[BatchItem]:
        """The results of an ended batch, in any order (match them by ``custom_id``). With
        ``json_output`` each answer is parsed as JSON (a batch of schema requests). The stream is
        closed however the iteration ends; a failure reading it raises :class:`LLMError`."""
        with _sdk_errors():
            results = await self._sdk().messages.batches.results(batch_id)
        try:
            async for item in _guarded(results):
                yield _batch_item(item, json_output=json_output)
        finally:
            await results.close()

    def _sdk(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            key = self._settings.api_key
            if key is None:
                raise LLMNotConfigured
            if "ANTHROPIC_CUSTOM_HEADERS" in os.environ:
                raise LLMError(
                    0, "ANTHROPIC_CUSTOM_HEADERS is set: unset it (the SDK would send it)"
                )
            # An explicit key: the SDK reads no ANTHROPIC_API_KEY, token or local profile.
            self._client = anthropic.AsyncAnthropic(
                api_key=key.get_secret_value(),
                base_url=self._settings.base_url,
                timeout=self._settings.timeout_seconds,
                max_retries=self._settings.max_retries,
                http_client=self._http,
            )
        return self._client

    def _batch_params(self, request: LLMRequest) -> MessageCreateParamsNonStreaming:
        params = MessageCreateParamsNonStreaming(
            model=self.model_for(request.tier),
            max_tokens=request.max_tokens,
            messages=[{"role": "user", "content": request.prompt}],
        )
        system, output_config = _system(request), _output_config(request)
        if system is not anthropic.omit:
            params["system"] = system
        if output_config is not anthropic.omit:
            params["output_config"] = output_config
        return params


@contextmanager
def _sdk_errors() -> Generator[None]:
    """The SDK's errors (after its retries), and failures reading a streamed answer, as ours."""
    try:
        yield
    except anthropic.APIConnectionError as exc:  # timeouts included
        raise LLMUnavailable(exc) from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(exc.status_code, exc.message) from exc
    except anthropic.APIResponseValidationError as exc:
        raise LLMError(502, f"an answer the SDK couldn't read ({exc.message})") from exc
    except httpx2.TransportError as exc:  # the connection broke mid-stream
        raise LLMUnavailable(exc) from exc
    except ValueError as exc:  # a streamed line that isn't JSON
        raise LLMError(502, f"an unreadable answer ({type(exc).__name__})") from exc


async def _guarded[T](items: AsyncIterator[T]) -> AsyncIterator[T]:
    """``items``, with a failure while streaming them raised as ours."""
    while True:
        with _sdk_errors():
            try:
                item = await anext(items)
            except StopAsyncIteration:
                return
        yield item


def _batch_item(item: MessageBatchIndividualResponse, *, json_output: bool) -> BatchItem:
    outcome = item.result
    if outcome.type == "errored":
        error = outcome.error.error
        # A request the API rejects stays rejected; anything else may succeed if sent again.
        status = 400 if error.type == "invalid_request_error" else 500
        return BatchItem(
            item.custom_id, "errored", error=LLMError(status, f"{error.type}: {error.message}")
        )
    if outcome.type != "succeeded":
        return BatchItem(item.custom_id, outcome.type)
    message = outcome.message
    usage = (_usage(message, batch=True),)
    category = message.stop_details.category if message.stop_details else None
    text = _text(block.text for block in message.content if block.type == "text")
    answer = _Answer(text, message.model, message.stop_reason, category, usage)
    try:
        result = _result(answer, json_output=json_output)
    except LLMError as error:
        return BatchItem(item.custom_id, "succeeded", error=error, usage=usage)
    return BatchItem(item.custom_id, "succeeded", result=result, usage=usage)


# For ceiling_usage: the message framing and the structured-output instructions the API adds.
CEILING_OVERHEAD_TOKENS = 512


def ceiling_usage(request: LLMRequest, model: str) -> LLMUsage:
    """The most ``request`` can be billed, for a call whose answer never came: a token a byte
    (UTF-8) of its prompt and schema (a token is never less than a byte, in any script) and
    :data:`CEILING_OVERHEAD_TOKENS`, no cache discount; its system prompt likewise, as a cache
    write when it is cached (the dearer rate); and ``max_tokens`` of output."""
    schema = json.dumps(request.schema) if request.schema is not None else ""
    system = len((request.system or "").encode())
    prompt = len((request.prompt + schema).encode()) + CEILING_OVERHEAD_TOKENS
    cached = request.cache_system and system > 0
    return LLMUsage(
        PROVIDER,
        model,
        input_tokens=prompt + (0 if cached else system),
        output_tokens=request.max_tokens,
        cache_write_tokens=system if cached else 0,
    )


def _system(request: LLMRequest) -> Any:
    """The ``system`` parameter: a text block, marked for caching if asked; or omitted."""
    if request.system is None:
        return anthropic.omit
    block: dict[str, Any] = {"type": "text", "text": request.system}
    if request.cache_system:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _output_config(request: LLMRequest) -> Any:
    """The ``output_config`` parameter: effort and the answer's JSON schema; or omitted."""
    config: dict[str, Any] = {}
    if request.effort is not None:
        config["effort"] = request.effort
    if request.schema is not None:
        config["format"] = {"type": "json_schema", "schema": dict(request.schema)}
    return config or anthropic.omit


def _text(parts: Iterable[str]) -> str:
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class _Answer:
    text: str
    model: str
    stop_reason: str | None
    refusal_category: str | None
    usage: tuple[LLMUsage, ...]


def _result(
    answer: _Answer,
    *,
    json_output: bool,
    max_tokens: int | None = None,
    request_id: str | None = None,
) -> LLMResult:
    if answer.stop_reason == "refusal":
        raise LLMRefused(answer.refusal_category)
    if answer.stop_reason == "max_tokens":
        raise LLMTruncated(max_tokens)
    data: Any = None
    if json_output:
        try:
            data = json.loads(answer.text)
        except ValueError as exc:
            raise LLMBadOutput(f"not JSON ({exc})") from exc
    return LLMResult(answer.text, data, answer.model, answer.usage, request_id)


def _usage(message: Message, *, batch: bool = False) -> LLMUsage:
    usage = message.usage
    return LLMUsage(
        PROVIDER,
        message.model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens or 0,
        usage.cache_creation_input_tokens or 0,
        batch=batch,
    )


def _beta_usage(message: BetaMessage, requested: str) -> tuple[LLMUsage, ...]:
    """One entry per model that ran: with a fallback, the declined attempt and the answer each
    bill at their own model's rates (an attempt that names no model was the one requested)."""
    usage = message.usage
    runs = [
        LLMUsage(
            PROVIDER,
            run.model or requested,
            run.input_tokens,
            run.output_tokens,
            run.cache_read_input_tokens,
            run.cache_creation_input_tokens,
        )
        for run in usage.iterations or []
        if isinstance(run, BetaMessageIterationUsage | BetaFallbackMessageIterationUsage)
    ]
    if runs:
        return tuple(runs)
    return (
        LLMUsage(
            PROVIDER,
            message.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_input_tokens or 0,
            usage.cache_creation_input_tokens or 0,
        ),
    )
