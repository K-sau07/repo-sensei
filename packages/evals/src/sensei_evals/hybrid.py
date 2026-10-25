"""The hybrid retriever: step 2.9's fusion (:mod:`sensei_engine.search.hybrid`) over a benchmark
checkout, indexed as the keyword retriever does it (:mod:`sensei_evals.keyword`).

Each question is routed by the rules (:func:`~sensei_engine.search.route.classify`: no model
call, so runs are free and repeatable) and planned (:mod:`sensei_engine.search.plan`); the plan's
legs run on the eval index, ``grep`` with ripgrep on the checkout. Semantic legs wait for 2.7.

Options (``--opt``):

* ``legs=lex_code`` (or a comma list): only those legs, of the plan the query would get if it
  were searched in full (a symbol or pattern query's short-circuit left aside), to measure one
  leg against the fusion;
* ``mode=hybrid``: every query searched in full, short-circuits left aside;
* ``w.<leg>=<weight>``, ``o.<origin>=<weight>``, ``multi_hit=``, ``layer_boost=``: weights.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from pathlib import Path

from sensei_core.config import get_settings
from sensei_core.db import make_database
from sensei_engine.search.hybrid import HybridRetriever, SearchHit, Weights, WorktreeGrep
from sensei_engine.search.plan import Leg, Mode, Origin, SearchPlan, plan
from sensei_engine.search.route import QueryClass, classify
from sensei_evals.keyword import KeywordRetriever, analyze_index
from sensei_evals.retrieval import Hit, RepoRef, RetrieverContext
from sensei_evals.symbols import EVAL_DATABASE, Sessions, eval_database_url

INDEXED_LEGS = frozenset({Leg.LEX_CODE, Leg.LEX_PATH, Leg.LEX_SUMMARY, Leg.SYM, Leg.GREP})


def plan_for(query: str, *, legs: frozenset[Leg] | None = None, full: bool = False) -> SearchPlan:
    """The plan of ``query`` as the rules route it; with ``legs``, only those legs of its full
    plan (``full``: the full plan, short-circuits left aside, also without ``legs``)."""
    route = classify(query).route
    if legs is not None or full:
        route = dataclasses.replace(route, query_class=QueryClass.CONCEPTUAL)
    found = plan(route)
    if legs is None:
        return found
    kept = tuple(sub for sub in found.legs if sub.leg in legs)
    return dataclasses.replace(found, mode=Mode.HYBRID, legs=kept)


def to_hit(hit: SearchHit) -> Hit:
    """As scored: its lines are the definition's and its snippet's (a chunk of several
    definitions shows them all)."""
    lines = (min(hit.start_line, hit.chunk_start), max(hit.end_line, hit.chunk_end))
    return Hit(path=hit.path, lines=lines, score=hit.score, symbol=hit.symbol_key)


def weights_from(options: dict[str, str]) -> Weights:
    base = Weights()
    legs = dict(base.legs)
    origins = dict(base.origins)
    changes: dict[str, float] = {}
    for key, value in options.items():
        if key.startswith("w."):
            legs[Leg(key[2:])] = float(value)
        elif key.startswith("o."):
            origins[Origin(key[2:])] = float(value)
        elif key in ("multi_hit", "layer_boost"):
            changes[key] = float(value)
    return dataclasses.replace(base, legs=legs, origins=origins, **changes)


class HybridEvalRetriever(KeywordRetriever):
    name = "hybrid"

    def __init__(
        self,
        sessions: Sessions,
        *,
        checkout: Path,
        legs: frozenset[Leg] | None = None,
        full: bool = False,
        weights: Weights | None = None,
        close: Callable[[], Awaitable[None]] | None = None,
        analyze: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(sessions, checkout=checkout, close=close, analyze=analyze)
        self._only = legs
        self._full = full
        self.hybrid = HybridRetriever(weights=weights, grep=WorktreeGrep(checkout))

    async def search(self, query: str, repo: RepoRef, k: int) -> list[Hit]:
        await self.prepare(repo)
        if self.snapshot is None:  # pragma: no cover - prepare set it or raised
            return []
        repo_id, snapshot_id = self.snapshot
        found = plan_for(query, legs=self._only, full=self._full)
        async with self.sessions() as db:
            result = await self.hybrid.search(
                db, found, repo_id=repo_id, snapshot_id=snapshot_id, k=k
            )
        return [to_hit(hit) for hit in result.hits]


def _legs(value: str | None) -> frozenset[Leg] | None:
    if value is None or value == "all":
        return None
    legs = frozenset(Leg(name.strip()) for name in value.split(","))
    if not legs <= INDEXED_LEGS:
        msg = f"legs are among {', '.join(sorted(INDEXED_LEGS))}"
        raise ValueError(msg)
    return legs


def checkout_hybrid(context: RetrieverContext) -> HybridEvalRetriever:
    """The ``hybrid`` retriever of ``sensei-eval run`` (see the module)."""
    if context.repo.checkout is None:
        msg = "the hybrid retriever needs a checkout of the repository (--checkout)"
        raise ValueError(msg)
    settings = get_settings().database
    name = context.options.get("db", EVAL_DATABASE)
    url = eval_database_url(settings.system_url.get_secret_value(), name)
    database = make_database(settings, url=url)
    owner_url = eval_database_url(settings.migrate_url.get_secret_value(), name)
    return HybridEvalRetriever(
        database.session,
        checkout=context.repo.checkout,
        legs=_legs(context.options.get("legs")),
        full=context.options.get("mode") == "hybrid",
        weights=weights_from(context.options),
        close=database.dispose,
        analyze=lambda: analyze_index(owner_url),
    )
