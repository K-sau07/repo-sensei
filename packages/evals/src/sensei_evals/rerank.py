"""The rerank stage (3.6.5) over the hybrid retriever's hits, as search runs it
(:func:`sensei_engine.search.service.rerank`), in the mode asked (``--opt rerank=interactive`` or
``agent``), within a hard budget (``--opt budget=<USD>``, required: nothing is spent without one).

* :class:`CostGuard`: every request billed, priced as the usage ledger prices it, and written to
  the eval database's ledger (each in its own transaction: a run stopped part way keeps what it
  paid). Past the budget the run stops (:class:`BudgetExceeded`), checked before and after each
  query's rerank (the sinks only count: a request billed is always recorded), so a run
  overshoots by at most one query's requests.
* :class:`CachedVoyageReranker`: the cross-encoder's scores of a query's candidates, kept on disk
  by (model, query, candidates). The agent mode's first stage is the interactive mode's, so the
  second mode run (or a run again) pays only for the model.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import orjson
from sqlalchemy import select

from sensei_core.config import VoyageSettings
from sensei_core.models import Org, UsageKind
from sensei_core.usage import CostCalculator, record_usage
from sensei_engine.llm.anthropic_client import LLMUsage
from sensei_engine.search.rerank import (
    MAX_CANDIDATES,
    VOYAGE_TOP_K,
    Candidate,
    Ranked,
    RerankUsage,
    RerankUsageSink,
    VoyageReranker,
)
from sensei_evals.symbols import EVAL_ORG, Sessions

REF_TYPE = "eval"


class BudgetExceeded(RuntimeError):
    """The run spent its budget (or met a model with no price, whose cost can't be kept)."""


@dataclass
class Spend:
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: Decimal = Decimal(0)


@dataclass
class CostGuard:
    """What a run paid, by model, within ``budget`` (USD); see the module."""

    budget: Decimal
    calculator: CostCalculator
    sessions: Sessions | None = None  # the ledger's (None: counted, not written)
    ref_id: str = ""  # the run's label in the ledger
    spent: Decimal = Decimal(0)
    by_model: dict[str, Spend] = field(default_factory=dict[str, Spend])
    unpriced: set[str] = field(default_factory=set[str])  # billed, but at no known price
    _org: uuid.UUID | None = None

    def check(self) -> None:
        """Raise :class:`BudgetExceeded` once the budget is spent, or a model of no price ran."""
        if self.unpriced:
            msg = f"no price for {', '.join(sorted(self.unpriced))}: the budget can't be kept"
            raise BudgetExceeded(msg)
        if self.spent >= self.budget:
            msg = f"the run spent ${self.spent} of its ${self.budget} budget"
            raise BudgetExceeded(msg)

    async def rerank_usage(self, usage: RerankUsage) -> None:
        await self._add(
            UsageKind.RERANK, usage.provider, usage.model, tokens_in=usage.tokens,
            units=usage.documents,
        )  # fmt: skip

    async def llm_usage(self, usage: LLMUsage) -> None:
        await self._add(
            UsageKind.LLM, usage.provider, usage.model, tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens, tokens_cache_read=usage.cache_read_tokens,
            tokens_cache_write=usage.cache_write_tokens,
        )  # fmt: skip

    async def _add(self, kind: UsageKind, provider: str, model: str, **counts: Any) -> None:
        units = counts.pop("units", 0)
        cost = self.calculator.cost(provider, model, **counts)
        if cost is None:
            self.unpriced.add(f"{provider}/{model}")
            cost = Decimal(0)
        spend = self.by_model.setdefault(f"{provider}/{model}", Spend())
        spend.requests += 1
        spend.tokens_in += counts.get("tokens_in", 0)
        spend.tokens_out += counts.get("tokens_out", 0)
        spend.cost += cost
        self.spent += cost
        if self.sessions is not None:
            async with self.sessions() as db:
                if self._org is None:
                    self._org = await db.scalar(select(Org.id).where(Org.slug == EVAL_ORG))
                if self._org is not None:
                    await record_usage(
                        db, self.calculator, org_id=self._org, kind=kind, provider=provider,
                        model=model, units=units, ref_type=REF_TYPE, ref_id=self.ref_id, **counts,
                    )  # fmt: skip

    def report(self) -> dict[str, Any]:
        return {
            "budget": str(self.budget),
            "spent": str(self.spent),
            "by_model": {
                model: {
                    "requests": s.requests,
                    "tokens_in": s.tokens_in,
                    "tokens_out": s.tokens_out,
                    "cost": str(s.cost),
                }
                for model, s in sorted(self.by_model.items())
            },
        }


class CachedVoyageReranker(VoyageReranker):
    """Voyage's cross-encoder, its scores kept in ``cache_dir`` (see the module). Every
    candidate is scored (Voyage bills the documents read, not those returned), so a cached
    answer serves any ``top_k``."""

    def __init__(
        self, settings: VoyageSettings, http: httpx.AsyncClient, *, cache_dir: Path, **kwargs: Any
    ) -> None:
        super().__init__(settings, http, **kwargs)
        self._dir = cache_dir
        self.cached = 0
        self.scored = 0

    async def rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        *,
        top_k: int = VOYAGE_TOP_K,
        on_usage: RerankUsageSink | None = None,
    ) -> list[Ranked]:
        shown = list(candidates[:MAX_CANDIDATES])
        if not shown or top_k <= 0:
            return []
        key = hashlib.sha256(
            orjson.dumps([self.model, query, [[c.id, c.document()] for c in shown]])
        ).hexdigest()
        path = self._dir / f"{key}.json"
        if path.is_file():
            scores: list[list[Any]] = orjson.loads(path.read_bytes())
            self.cached += 1
        else:
            ranked = await super().rerank(query, shown, top_k=len(shown), on_usage=on_usage)
            scores = [[r.candidate.id, r.score] for r in ranked]
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(orjson.dumps(scores))
            self.scored += 1
        by_id = {c.id: c for c in shown}
        return [Ranked(by_id[str(i)], score) for i, score in scores[:top_k] if str(i) in by_id]
