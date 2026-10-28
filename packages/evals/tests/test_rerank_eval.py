"""The rerank stage of the hybrid eval (3.6.5), against fake Voyage and Anthropic APIs: one
cross-encoder call serves both modes (cached on disk), and the cost guard stops a run past its
budget, the ledger keeping what it paid."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import VoyageSettings
from sensei_core.models import UsageEvent
from sensei_core.usage import DEFAULT_PRICE_TABLE, CostCalculator
from sensei_engine.llm.anthropic_client import LLMUsage
from sensei_engine.search.rerank import (
    Candidate,
    LLMReranker,
    Reranker,
    RerankMode,
    RerankUsage,
)
from sensei_evals.hybrid import HybridEvalRetriever
from sensei_evals.rerank import BudgetExceeded, CachedVoyageReranker, CostGuard
from sensei_evals.retrieval import RepoRef
from sensei_testing.anthropic import FakeAnthropic, message

URL = "https://api.voyageai.com/v1/rerank"
QUERY = "where is an item saved?"
SHA = "f" * 40
CALCULATOR = CostCalculator(DEFAULT_PRICE_TABLE)


async def _no_sleep(_: float) -> None:
    return None


def _scored(request: httpx.Request) -> httpx.Response:
    """Scores a document by the number in it (``f7`` scores 0.07), else 0.01."""
    body: dict[str, Any] = json.loads(request.content)
    scores: list[tuple[int, float]] = []
    documents: list[str] = body["documents"]
    for i, doc in enumerate(documents):
        found = re.search(r"f(\d+)", doc)
        scores.append((i, int(found[1]) / 100 if found else 0.01))
    scores.sort(key=lambda item: -item[1])
    data = [{"index": i, "relevance_score": s} for i, s in scores[: body["top_k"]]]
    return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 1000}})


def _model() -> FakeAnthropic:
    """Picks the shown candidates in reverse."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        text = json.loads(request.content)["messages"][0]["content"]
        text = text if isinstance(text, str) else text[0]["text"]
        inside = text.split("<candidates>\n", 1)[1].split("\n</candidates>", 1)[0]
        handles = [json.loads(line)["id"] for line in inside.splitlines()]
        ranking = [{"id": h, "grade": 2, "reason": ""} for h in reversed(handles)]
        return httpx2.Response(
            200, json=message(json.dumps({"ranking": ranking}), model="claude-haiku-5-5")
        )

    return FakeAnthropic({"/v1/messages": answer})


def _voyage(http: httpx.AsyncClient, cache: Path) -> CachedVoyageReranker:
    settings = VoyageSettings(api_key=SecretStr("pa-test"))
    return CachedVoyageReranker(settings, http, cache_dir=cache, sleep=_no_sleep)


def _candidates(count: int) -> list[Candidate]:
    return [Candidate(id=str(i), code=f"def f{i}():\n    pass\n") for i in range(count)]


@respx.mock
async def test_one_cross_encoder_call_serves_both_modes(tmp_path: Path) -> None:
    route = respx.post(URL).mock(side_effect=_scored)
    guard = CostGuard(Decimal(1), CALCULATOR)
    async with httpx.AsyncClient() as http:
        voyage = _voyage(http, tmp_path)
        reranker = Reranker(voyage, LLMReranker(_model().client()))
        quick = await reranker.rerank(
            QUERY, _candidates(60), RerankMode.INTERACTIVE, on_rerank_usage=guard.rerank_usage
        )
        assert [r.candidate.id for r in quick.items[:3]] == ["59", "58", "57"]
        agent = await reranker.rerank(
            QUERY,
            _candidates(60),
            RerankMode.AGENT,
            on_rerank_usage=guard.rerank_usage,
            on_llm_usage=guard.llm_usage,
        )
    assert route.call_count == 1  # the agent's first stage came from the cache
    assert (voyage.scored, voyage.cached) == (1, 1)
    assert agent.source == "llm"
    assert len(agent.items) == 20
    # Billed once for the cross-encoder, then for the model's windows only.
    assert guard.by_model["voyage/rerank-3"].requests == 1
    assert guard.by_model["anthropic/claude-haiku-5-5"].requests >= 1
    assert guard.spent == sum((s.cost for s in guard.by_model.values()), Decimal(0))


async def test_the_guard_stops_past_its_budget_or_without_a_price() -> None:
    guard = CostGuard(Decimal("0.0001"), CALCULATOR)
    guard.check()  # nothing spent yet
    await guard.rerank_usage(RerankUsage("voyage", "rerank-3", 10, 1000))  # $0.00005
    guard.check()
    await guard.llm_usage(LLMUsage("anthropic", "claude-haiku-5-5", 1000, 100))  # $0.00015
    with pytest.raises(BudgetExceeded, match="spent"):
        guard.check()
    unknown = CostGuard(Decimal(1), CALCULATOR)
    await unknown.llm_usage(LLMUsage("anthropic", "claude-unknown", 1, 1))  # still counted
    assert unknown.by_model["anthropic/claude-unknown"].requests == 1
    with pytest.raises(BudgetExceeded, match="no price"):
        unknown.check()


def test_reranking_needs_a_budget(tmp_path: Path) -> None:
    reranker = cast("Reranker", object())
    with pytest.raises(ValueError, match="needs a budget"):
        HybridEvalRetriever(cast("Any", None), checkout=tmp_path, reranker=reranker)


def _sessions(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def sessions() -> AsyncGenerator[AsyncSession]:
        yield session

    return sessions


FILES = {f"app/m{i}.py": f"def f{i}(item):\n    return save(item)\n" for i in range(1, 6)}


@pytest.mark.integration
@respx.mock
async def test_a_run_past_its_budget_stops_and_the_ledger_keeps_what_it_paid(
    system_session: AsyncSession, tmp_path: Path
) -> None:
    respx.post(URL).mock(side_effect=_scored)
    checkout = tmp_path / "repo"
    for path, content in FILES.items():
        (checkout / path).parent.mkdir(parents=True, exist_ok=True)
        (checkout / path).write_text(content, encoding="utf-8")
    sessions = _sessions(system_session)
    # One query's cross-encoder ($0.00005) and the model's window ($0.00002) pass it.
    guard = CostGuard(Decimal("0.00005"), CALCULATOR, sessions=sessions, ref_id="tiny:agent")
    async with httpx.AsyncClient() as http:
        voyage = _voyage(http, tmp_path / "cache")
        retriever = HybridEvalRetriever(
            sessions,
            checkout=checkout,
            reranker=Reranker(voyage, LLMReranker(_model().client())),
            rerank_mode=RerankMode.AGENT,
            guard=guard,
            cross_encoder=voyage,
        )
        repo = RepoRef(id="tiny-rerank", sha=SHA, checkout=checkout)
        with pytest.raises(BudgetExceeded):  # the first query's bill passes it
            await retriever.search("save item", repo, 5)
        with pytest.raises(BudgetExceeded):  # the next is refused before it pays
            await retriever.search("where is an item saved", repo, 5)
    assert voyage.scored == 1  # one query paid for
    report = retriever.report()["rerank"]
    assert isinstance(report, dict)
    assert report["mode"] == "agent"
    rows = (
        await system_session.scalars(select(UsageEvent).where(UsageEvent.ref_id == "tiny:agent"))
    ).all()
    assert {row.model for row in rows} == {"rerank-3", "claude-haiku-5-5"}
    assert sum((row.cost_usd for row in rows), Decimal(0)) == guard.spent
