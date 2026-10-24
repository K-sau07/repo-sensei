"""Reranking search candidates (2.11): the best first, in two stages, on plain data. There is no
database here: the retriever (2.9) hands candidates in and takes the order back.

1. :class:`VoyageReranker` is a cross-encoder (``rerank-3``, or ``rerank-3-lite`` for the cheap
   tier). It scores each candidate against the query on its own: the top 150 in, 30-50 out.
   - Candidates go in requests of at most 1000 documents and 600K tokens (Voyage's limits; the
     query counts once per document), all sent at once.
   - A score doesn't depend on the other documents in its request, so the answers merge.
   - What each request was billed goes to ``on_usage``.
2. :class:`LLMReranker` is listwise: Claude (Haiku, or Sonnet in deep mode) reads the query and
   the survivors together and picks the best 10-20 in order, each with a grade and a one-line
   reason.
   - The candidates are split into windows, dealt round robin so each window holds strong and
     weak ones, and the windows are ranked in parallel. That keeps each answer short and the
     stage fast.
   - The picks merge by grade, then by rank within their window.
   - The model sees short handles (``c1``, ``c2`` ...) instead of the candidates' ids, and every
     handle it answers with is checked against the window it was shown. Unknown and repeated
     handles are dropped, and if too few picks remain, the first stage's order fills the rest.
   - A window that fails or is too slow keeps the first stage's order.

Both stages see each candidate's code cut to its first :data:`MAX_CODE_LINES` lines, with long
lines cut to :data:`MAX_LINE_CHARS`.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from importlib import resources
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import VoyageSettings
from sensei_core.logging import get_logger
from sensei_core.models import UsageKind
from sensei_core.usage import CostCalculator, record_usage
from sensei_engine.embed.voyage import (
    RateLimiter,
    Sleep,
    VoyageAPI,
    VoyageError,
    VoyageNotConfigured,
    billed_tokens,
    estimate_tokens,
    plan_batches,
)
from sensei_engine.llm.anthropic_client import (
    AnthropicClient,
    LLMError,
    LLMRequest,
    LLMUsageSink,
    Role,
    tier_for,
)

log = get_logger(__name__)

MAX_CODE_LINES = 40
MAX_LINE_CHARS = 200

# Voyage's limits per rerank request (with room: our token count is an estimate).
MAX_DOCUMENTS = 1000
MAX_REQUEST_TOKENS = 500_000
VOYAGE_TOP_K = 40  # 30-50 survive the first stage

PROMPT_VERSION = "rerank_listwise.v1"
LLM_TOP_N = 15  # 10-20 come out of the second stage
WINDOW_SIZE = 12  # candidates per model call
TIMEOUT_SECONDS = 6.0
MAX_TOKENS = 4_000  # the answer is ~50 tokens a pick; the rest is room for thinking
MAX_REASON_CHARS = 200


@dataclass(frozen=True, slots=True)
class Candidate:
    """One search result to rank. ``id`` is the caller's (a node id, ``path:line``)."""

    id: str
    code: str
    path: str | None = None
    layer: str | None = None
    signature: str | None = None
    graph_neighbourhood: str | None = None  # callers and callees, filled in by 3.6

    def excerpt(self) -> str:
        """The code as the rerankers see it: the first lines, long lines cut."""
        lines = self.code.splitlines()[:MAX_CODE_LINES]
        return "\n".join(line[:MAX_LINE_CHARS] for line in lines)

    def document(self) -> str:
        """What the cross-encoder reads: where the code is, then the code."""
        head = " ".join(part for part in (self.path, self.signature) if part)
        return f"{head}\n{self.excerpt()}" if head else self.excerpt()


@dataclass(frozen=True, slots=True)
class Ranked:
    candidate: Candidate
    score: float | None = None  # the cross-encoder's relevance, 0-1
    grade: int | None = None  # the model's: 3 answers the query ... 0 unrelated
    reason: str | None = None  # the model's, one line


@dataclass(frozen=True, slots=True)
class Ranking:
    items: tuple[Ranked, ...]
    # llm: every window answered; partial: some kept the first stage's order; fallback: none did
    source: Literal["llm", "partial", "fallback"]
    dropped: int = 0  # handles the model gave that it wasn't shown, or gave twice


def _check_ids(candidates: Sequence[Candidate]) -> None:
    ids = [candidate.id for candidate in candidates]
    if len(set(ids)) != len(ids):
        msg = "candidate ids must be unique"
        raise ValueError(msg)


# ---------------------------------------------------------------- voyage


@dataclass(frozen=True, slots=True)
class RerankUsage:
    """One rerank request, as the provider billed it."""

    provider: str
    model: str
    documents: int
    tokens: int


RerankUsageSink = Callable[[RerankUsage], Awaitable[None]]


def usage_ledger(
    db: AsyncSession,
    calculator: CostCalculator,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> RerankUsageSink:
    """A sink that records each rerank request in the usage ledger, priced (the session must be
    scoped to ``org_id``)."""

    async def record(usage: RerankUsage) -> None:
        await record_usage(
            db,
            calculator,
            org_id=org_id,
            kind=UsageKind.RERANK,
            provider=usage.provider,
            model=usage.model,
            user_id=user_id,
            tokens_in=usage.tokens,
            units=usage.documents,
            ref_type=ref_type,
            ref_id=ref_id,
        )

    return record


@dataclass(slots=True)
class _Scores:
    by_index: dict[int, float] = field(default_factory=dict[int, float])
    billed: list[RerankUsage] = field(default_factory=list[RerankUsage])


class VoyageReranker:
    """Voyage's cross-encoder: a relevance score per candidate."""

    def __init__(
        self,
        settings: VoyageSettings,
        http: httpx.AsyncClient,
        *,
        lite: bool = False,
        limiter: RateLimiter | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        # Rate limits are counted apart from the embeddings' (a limiter of its own).
        self._api = VoyageAPI(settings, http, limiter=limiter, sleep=sleep, jitter=jitter)
        self.model = settings.rerank_lite_model if lite else settings.rerank_model

    @property
    def provider(self) -> str:
        return "voyage"

    @property
    def configured(self) -> bool:
        return self._settings.configured

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        *,
        top_k: int = VOYAGE_TOP_K,
        on_usage: RerankUsageSink | None = None,
    ) -> list[Ranked]:
        """The ``top_k`` most relevant candidates, best first, each with its score. Raises
        :class:`~sensei_engine.embed.voyage.VoyageError` (the caller keeps its own order)."""
        _check_ids(candidates)
        if not candidates or top_k <= 0:
            return []
        if not self.configured:
            raise VoyageNotConfigured
        documents = [candidate.document() for candidate in candidates]
        query_tokens = estimate_tokens(query)
        estimates = [query_tokens + estimate_tokens(document) for document in documents]
        batches = plan_batches(
            estimates,
            max_texts=MAX_DOCUMENTS,
            max_tokens=min(MAX_REQUEST_TOKENS, self._settings.tokens_per_minute),
        )
        scores = _Scores()
        gate = asyncio.Semaphore(self._settings.max_concurrency)

        async def run(batch: range) -> None:
            async with gate:
                await self._request(
                    query, documents, batch, top_k=top_k, estimates=estimates, scores=scores
                )

        try:
            async with asyncio.TaskGroup() as group:
                for batch in batches:
                    group.create_task(run(batch))
        except ExceptionGroup as failed:  # the first failure; it cancelled the other requests
            raise failed.exceptions[0] from None
        finally:
            # Reported here, not in the requests: a cancelled one must not stop a sink part way.
            if on_usage is not None:
                for usage in scores.billed:
                    await on_usage(usage)
        best = sorted(scores.by_index.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return [Ranked(candidates[index], score=score) for index, score in best]

    async def _request(
        self,
        query: str,
        documents: list[str],
        batch: range,
        *,
        top_k: int,
        estimates: list[int],
        scores: _Scores,
    ) -> None:
        expected = min(top_k, len(batch))
        body = {
            "query": query,
            "documents": [documents[i] for i in batch],
            "model": self.model,
            "top_k": expected,
            "truncation": True,
        }
        answer = await self._api.post(
            "rerank",
            body,
            tokens=sum(estimates[i] for i in batch),
            timeout_seconds=self._settings.rerank_timeout_seconds,
            max_retries=self._settings.rerank_max_retries,
        )
        tokens = billed_tokens(answer)
        scores.billed.append(RerankUsage(self.provider, self.model, len(batch), tokens))
        data = answer.get("data")
        if not isinstance(data, list) or len(cast("list[Any]", data)) != expected:
            raise VoyageError(502, f"expected {expected} rerank results in the answer")
        found: dict[int, float] = {}
        for raw in cast("list[Any]", data):
            item = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
            index, score = item.get("index"), item.get("relevance_score")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(batch)
                or index in found
            ):
                raise VoyageError(502, "a rerank result with a bad index")
            if (
                not isinstance(score, int | float)
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise VoyageError(502, "a rerank result without a score")
            found[index] = float(score)
        for index, score in found.items():
            scores.by_index[batch.start + index] = score


# ---------------------------------------------------------------- the model, listwise


class _Pick(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    grade: int = Field(ge=0, le=3)
    reason: str = ""


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ranking: list[_Pick]


def answer_schema() -> dict[str, Any]:
    """The JSON schema the model answers in."""
    pick = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "grade": {"type": "integer", "enum": [0, 1, 2, 3]},
            "reason": {"type": "string"},
        },
        "required": ["id", "grade", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"ranking": {"type": "array", "items": pick}},
        "required": ["ranking"],
        "additionalProperties": False,
    }


@cache
def system_prompt() -> str:
    path = resources.files("sensei_engine.search").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


def windows(count: int, size: int) -> list[list[int]]:
    """``range(count)`` dealt round robin into ``ceil(count / size)`` windows, so each holds
    some of the first stage's best."""
    number = max(1, math.ceil(count / size))
    return [list(range(start, count, number)) for start in range(number)]


def render(query: str, shown: Sequence[tuple[str, Candidate]], picks: int) -> str:
    """The user message: the query and each candidate as one JSON object (code is data)."""
    lines = [f"<query>{json.dumps(query)}</query>", "<candidates>"]
    for handle, candidate in shown:
        entry: dict[str, str] = {"id": handle}
        for key, value in (
            ("path", candidate.path),
            ("layer", candidate.layer),
            ("signature", candidate.signature),
            ("graph_neighbourhood", candidate.graph_neighbourhood),
        ):
            if value:
                entry[key] = value
        entry["code"] = candidate.excerpt()
        lines.append(json.dumps(entry, ensure_ascii=False))
    lines.append("</candidates>")
    lines.append(f"Pick at most {picks}, best first.")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _Window:
    picks: list[tuple[int, _Pick]]  # (candidate index, pick), in the model's order
    dropped: int
    answered: bool


class LLMReranker:
    """Claude ranks the candidates listwise; see the module."""

    def __init__(
        self,
        client: AnthropicClient,
        *,
        window_size: int = WINDOW_SIZE,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ) -> None:
        if window_size < 2:
            msg = "a window holds at least two candidates"
            raise ValueError(msg)
        self._client = client
        self._window_size = window_size
        self._timeout = timeout_seconds

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate | Ranked],
        *,
        top_n: int = LLM_TOP_N,
        deep: bool = False,
        on_usage: LLMUsageSink | None = None,
    ) -> Ranking:
        """The best ``top_n`` of ``candidates`` (given in the first stage's order), best first.
        Never raises for the model's sake: a window that fails keeps the first stage's order."""
        ranked = [c if isinstance(c, Ranked) else Ranked(c) for c in candidates]
        _check_ids([r.candidate for r in ranked])
        if top_n <= 0 or not ranked:
            return Ranking((), "fallback")
        if len(ranked) == 1 or not self._client.configured:
            return Ranking(tuple(ranked[:top_n]), "fallback")
        groups = windows(len(ranked), self._window_size)
        per_window = min(self._window_size, math.ceil(top_n * 1.5 / len(groups)))
        role = Role.RERANK_DEEP if deep else Role.RERANK
        results = await asyncio.gather(
            *(
                self._window(query, ranked, group, picks=per_window, role=role, on_usage=on_usage)
                for group in groups
            )
        )
        return _merge(ranked, results, top_n)

    async def _window(
        self,
        query: str,
        ranked: list[Ranked],
        group: list[int],
        *,
        picks: int,
        role: Role,
        on_usage: LLMUsageSink | None,
    ) -> _Window:
        handles = {f"c{index + 1}": index for index in group}
        shown = [(handle, ranked[index].candidate) for handle, index in handles.items()]
        request = LLMRequest(
            render(query, shown, picks),
            system=system_prompt(),
            tier=tier_for(role),
            max_tokens=MAX_TOKENS,
            effort="low",
            schema=answer_schema(),
            cache_system=True,
        )
        try:
            async with asyncio.timeout(self._timeout):
                result = await self._client.complete(request, on_usage=on_usage)
            answer = _Answer.model_validate(result.data)
        except (LLMError, TimeoutError, ValidationError) as exc:
            log.warning("listwise rerank kept the first stage's order", error=type(exc).__name__)
            return _Window([], 0, answered=False)
        chosen: list[tuple[int, _Pick]] = []
        seen: set[int] = set()
        dropped = 0
        for pick in answer.ranking:
            index = handles.get(pick.id.strip())
            if index is None or index in seen:
                dropped += 1
                continue
            seen.add(index)
            chosen.append((index, pick))
        return _Window(chosen[:picks], dropped, answered=True)


def _merge(ranked: list[Ranked], results: Sequence[_Window], top_n: int) -> Ranking:
    """The picks by grade, then rank in their window, then the first stage's order; then the
    unpicked candidates of windows that didn't answer, then the rest, in the first stage's order."""
    picked: list[tuple[int, int, int, Ranked]] = []
    for result in results:
        for rank, (index, pick) in enumerate(result.picks):
            reason = " ".join(pick.reason.split())[:MAX_REASON_CHARS] or None
            item = replace(ranked[index], grade=pick.grade, reason=reason)
            picked.append((-pick.grade, rank, index, item))
    picked.sort(key=lambda entry: entry[:3])
    order = [entry[3] for entry in picked]
    taken = {entry[2] for entry in picked}
    order += [r for i, r in enumerate(ranked) if i not in taken]
    answered = sum(result.answered for result in results)
    source: Literal["llm", "partial", "fallback"] = (
        "llm" if answered == len(results) else "partial" if answered else "fallback"
    )
    dropped = sum(result.dropped for result in results)
    return Ranking(tuple(order[:top_n]), source, dropped)
