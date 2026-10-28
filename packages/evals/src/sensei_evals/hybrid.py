"""The hybrid retriever: step 2.9's fusion (:mod:`sensei_engine.search.hybrid`) over a benchmark
checkout, indexed as the keyword retriever does it (:mod:`sensei_evals.keyword`).

Each question is routed by the rules (:func:`~sensei_engine.search.route.classify`: no model
call, so runs are free and repeatable) and planned (:mod:`sensei_engine.search.plan`); the plan's
legs run on the eval index, ``grep`` with ripgrep on the checkout, and the semantic legs on the
index's embeddings (:class:`~sensei_engine.search.dense.EmbeddedLegs`): with a Voyage key, the
index is embedded as the embeddings stage does it (through the org's cache, so a second run of
the same code pays only for its questions); without one, the semantic legs are left out.

Options (``--opt``):

* ``legs=lex_code`` (or a comma list): only those legs, of the plan the query would get if it
  were searched in full (a symbol or pattern query's short-circuit left aside), to measure one
  leg against the fusion;
* ``mode=hybrid``: every query searched in full, short-circuits left aside;
* ``w.<leg>=<weight>``, ``o.<origin>=<weight>``, ``multi_hit=``, ``layer_boost=``: weights;
* ``semantic=0``: no semantic legs, even with a Voyage key;
* ``graph=1``: the graph leg (``ppr``, 3.6), on the index linked as the graph stage links it
  (:mod:`sensei_evals.graph`; ``w.ppr=`` weighs it);
* ``reuse=1``: search the eval database's index of the pinned commit as it is, when it is
  chunked and fully embedded, instead of indexing the checkout again (a langflow index and its
  embeddings take most of an hour);
* ``rerank=interactive`` or ``agent``: the best :data:`RERANK_CANDIDATES` hits reranked as
  search reranks them, in that mode (:mod:`sensei_evals.rerank`), within ``budget=<USD>``
  (required: paid calls), the cross-encoder's scores kept in ``rerank_cache=`` (default
  ``.cache/eval-rerank``) for the other mode and runs again.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import select

from sensei_core.config import get_settings
from sensei_core.db import make_database
from sensei_core.models import Org, Repo, Snapshot
from sensei_core.usage import CostCalculator
from sensei_engine.embed import store
from sensei_engine.embed.provider import EmbeddingProvider, EmbeddingUsage
from sensei_engine.embed.voyage import VoyageClient
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.llm.anthropic_client import AnthropicClient
from sensei_engine.search.dense import EmbeddedLegs
from sensei_engine.search.hybrid import HybridRetriever, SearchHit, Weights, WorktreeGrep
from sensei_engine.search.plan import Leg, Mode, Origin, SearchPlan, plan
from sensei_engine.search.rerank import LLMReranker, Reranker, RerankMode
from sensei_engine.search.route import QueryClass, classify
from sensei_engine.search.service import RERANK_CANDIDATES, read_contents, rerank
from sensei_evals.dense import embed_snapshot
from sensei_evals.graph import link_snapshot
from sensei_evals.keyword import KeywordRetriever, analyze_index
from sensei_evals.paths import repo_root
from sensei_evals.rerank import CachedVoyageReranker, CostGuard
from sensei_evals.retrieval import Hit, RepoRef, RetrieverContext
from sensei_evals.symbols import EVAL_DATABASE, EVAL_ORG, Sessions, eval_database_url

INDEXED_LEGS = frozenset({Leg.LEX_CODE, Leg.LEX_PATH, Leg.LEX_SUMMARY, Leg.SYM, Leg.GREP})
SEMANTIC_LEGS = frozenset({Leg.SEM_CODE, Leg.SEM_SUMMARY})


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
        embeddings: EmbeddingProvider | None = None,
        reuse: bool = False,
        graph: bool = False,
        reranker: Reranker | None = None,
        rerank_mode: RerankMode = RerankMode.INTERACTIVE,
        guard: CostGuard | None = None,
        cross_encoder: CachedVoyageReranker | None = None,  # the reranker's, for the report
        close: Callable[[], Awaitable[None]] | None = None,
        analyze: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(sessions, checkout=checkout, close=close, analyze=analyze)
        self._checkout_path = checkout
        self._only = legs
        self._full = full
        self._embeddings = embeddings
        self._reuse = reuse
        self._link = graph
        self._dense = (
            EmbeddedLegs(embeddings, on_usage=self._bill) if embeddings is not None else None
        )
        self.hybrid = HybridRetriever(
            weights=weights, grep=WorktreeGrep(checkout), dense=self._dense
        )
        self.tokens = 0  # what Voyage billed: the index's texts and the questions
        self.cached: int | None = None
        self.embedded: int | None = None
        self.reused = False
        self.graph: tuple[int, int] | None = None  # the linked graph's nodes and edges
        self._graph: GraphSnapshot | None = None
        if reranker is not None and guard is None:
            msg = "reranking is paid: it needs a budget (a CostGuard)"
            raise ValueError(msg)
        self._reranker = reranker
        self._rerank_mode = rerank_mode
        self.guard = guard
        self._cross_encoder = cross_encoder

    async def _bill(self, usage: EmbeddingUsage) -> None:
        self.tokens += usage.tokens

    async def prepare(self, repo: RepoRef) -> None:
        """Index (or, with ``reuse``, find the index of) the checkout, then embed it; with
        ``graph``, link it."""
        if self.snapshot is not None:
            return
        found = await ready_snapshot(self.sessions, repo) if self._reuse else None
        if found is not None:
            self._snapshot = found
            self.reused = True
        else:
            await super().prepare(repo)
            if self._embeddings is not None and self.snapshot is not None:
                self.cached, self.embedded, _ = await embed_snapshot(
                    self.sessions, self._embeddings, self.snapshot[1], on_usage=self._bill
                )
        if self._link and self.snapshot is not None:
            graph = await link_snapshot(self.sessions, self.snapshot[1], self._checkout_path)
            self.graph = (len(graph.nodes), len(graph.edges))
            self._graph = graph
            self.hybrid = HybridRetriever(
                weights=self.hybrid.weights,
                grep=WorktreeGrep(self._checkout_path),
                dense=self._dense,
                graph=graph,
            )

    async def search(self, query: str, repo: RepoRef, k: int) -> list[Hit]:
        await self.prepare(repo)
        if self.snapshot is None:  # pragma: no cover - prepare set it or raised
            return []
        repo_id, snapshot_id = self.snapshot
        found = plan_for(query, legs=self._only, full=self._full)
        depth = max(k, RERANK_CANDIDATES) if self._reranker is not None else k
        async with self.sessions() as db:
            result = await self.hybrid.search(
                db, found, repo_id=repo_id, snapshot_id=snapshot_id, k=depth
            )
            hits = list(result.hits)
            if self._reranker is not None and self.guard is not None:
                self.guard.check()  # before paying for this query
                top = hits[:RERANK_CANDIDATES]
                contents = await read_contents(db, repo_id, [hit.chunk_id for hit in top])
                hits, _, _ = await rerank(
                    self._reranker,
                    query,
                    hits,
                    contents,
                    candidates=RERANK_CANDIDATES,
                    on_usage=self.guard.rerank_usage,
                    graph=self._graph,
                    mode=self._rerank_mode,
                    on_llm_usage=self.guard.llm_usage,
                )
                self.guard.check()  # past the budget: the run stops here
        return [to_hit(hit) for hit in hits[:k]]

    def report(self) -> Mapping[str, object]:
        """What Voyage was paid and what the cache held, for the run's notes."""
        return {
            "tokens": self.tokens,
            "cached": self.cached,
            "embedded": self.embedded,
            "reused": self.reused,
            "semantic": self._embeddings is not None,
            "graph": self.graph,
            "rerank": self._rerank_report(),
        }

    def _rerank_report(self) -> Mapping[str, object] | None:
        if self._reranker is None or self.guard is None:
            return None
        voyage = self._cross_encoder
        cache = {"cached": voyage.cached, "scored": voyage.scored} if voyage else None
        return {"mode": self._rerank_mode.value, "cross_encoder": cache, **self.guard.report()}


async def ready_snapshot(sessions: Sessions, repo: RepoRef) -> tuple[uuid.UUID, uuid.UUID] | None:
    """The eval database's snapshot of ``repo`` at its pinned commit, if it is chunked and every
    live chunk has its vector: ``(repo id, snapshot id)``."""
    async with sessions() as db:
        found = (
            await db.execute(
                select(Snapshot.repo_id, Snapshot.id)
                .join(Repo, Repo.id == Snapshot.repo_id)
                .join(Org, Org.id == Repo.org_id)
                .where(
                    Org.slug == EVAL_ORG,
                    Repo.slug == repo.id,
                    Snapshot.commit_sha == repo.sha,
                    Snapshot.chunk_generation.is_not(None),
                )
                .order_by(Snapshot.created_at.desc())
                .limit(1)
            )
        ).first()
        if found is None or await store.count_pending(db, found[1]):
            return None
        return found[0], found[1]


def _legs(value: str | None, *, semantic: bool) -> frozenset[Leg] | None:
    if value is None or value == "all":
        return None
    legs = frozenset(Leg(name.strip()) for name in value.split(","))
    allowed = INDEXED_LEGS | SEMANTIC_LEGS if semantic else INDEXED_LEGS
    if not legs <= allowed:
        msg = f"legs are among {', '.join(sorted(allowed))}"
        raise ValueError(msg)
    return legs


def _rerank_mode(value: str | None) -> RerankMode | None:
    if value is None or value in {"", "0", "none"}:
        return None
    modes = (RerankMode.INTERACTIVE, RerankMode.AGENT)  # deep (Sonnet) waits for 4.14
    if value not in {m.value for m in modes}:
        msg = f"rerank is one of {', '.join(m.value for m in modes)}"
        raise ValueError(msg)
    return RerankMode(value)


def checkout_hybrid(context: RetrieverContext) -> HybridEvalRetriever:
    """The ``hybrid`` retriever of ``sensei-eval run`` (see the module)."""
    if context.repo.checkout is None:
        msg = "the hybrid retriever needs a checkout of the repository (--checkout)"
        raise ValueError(msg)
    settings = get_settings()
    database_settings = settings.database
    name = context.options.get("db", EVAL_DATABASE)
    url = eval_database_url(database_settings.system_url.get_secret_value(), name)
    database = make_database(database_settings, url=url)
    owner_url = eval_database_url(database_settings.migrate_url.get_secret_value(), name)
    semantic = settings.voyage.configured and context.options.get("semantic", "1") != "0"
    mode = _rerank_mode(context.options.get("rerank"))
    http = httpx.AsyncClient() if semantic or mode is not None else None
    reranker: Reranker | None = None
    guard: CostGuard | None = None
    voyage: CachedVoyageReranker | None = None
    if mode is not None and http is not None:
        budget = context.options.get("budget")
        if budget is None or Decimal(budget) <= 0:
            msg = "reranking is paid: give it a budget (--opt budget=<USD>)"
            raise ValueError(msg)
        if not settings.voyage.configured:
            msg = "reranking needs a Voyage API key"
            raise ValueError(msg)
        if mode is RerankMode.AGENT and not settings.anthropic.configured:
            msg = "the agent mode's rerank needs an Anthropic API key"
            raise ValueError(msg)
        cache = Path(context.options.get("rerank_cache") or repo_root() / ".cache" / "eval-rerank")
        voyage = CachedVoyageReranker(settings.voyage, http, cache_dir=cache)
        reranker = Reranker(voyage, LLMReranker(AnthropicClient(settings.anthropic)))
        guard = CostGuard(
            Decimal(budget),
            CostCalculator.from_settings(settings.pricing),
            sessions=database.session,
            ref_id=f"{context.suite.name}:{mode.value}",
        )

    async def close() -> None:
        if http is not None:
            await http.aclose()
        await database.dispose()

    return HybridEvalRetriever(
        database.session,
        checkout=context.repo.checkout,
        legs=_legs(context.options.get("legs"), semantic=semantic),
        full=context.options.get("mode") == "hybrid",
        weights=weights_from(context.options),
        embeddings=VoyageClient(settings.voyage, http) if semantic and http is not None else None,
        reuse=context.options.get("reuse") in {"1", "true", "yes"},
        graph=context.options.get("graph") in {"1", "true", "yes"},
        reranker=reranker,
        rerank_mode=mode or RerankMode.INTERACTIVE,
        guard=guard,
        cross_encoder=voyage,
        close=close,
        analyze=lambda: analyze_index(owner_url),
    )
