"""The semantic retrievers: step 2.7's dense search (:mod:`sensei_engine.search.dense`) over a
benchmark checkout's chunks, alone (``dense``) or fused with 2.6's BM25 legs (``hybrid``).

The checkout is indexed as the keyword retriever does it (:class:`~sensei_evals.keyword.
KeywordRetriever`), then embedded as the embeddings stage does: every chunk's text through the
org's embedding cache (by content hash), Voyage for the rest, the vectors written to the chunks,
then the partition's HNSW index. A second run on the same code finds every vector in the cache.
Each question is embedded as a query.

``hybrid`` fuses the dense code leg with both BM25 legs by reciprocal rank (k = 60), each leg
asked for :data:`POOL` chunks, all read in one repeatable-read transaction: what 2.9's retriever
starts from, before routing and reranking.
What Voyage was paid is counted (:attr:`SemanticRetriever.tokens`).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence

import httpx

from sensei_core.config import get_settings
from sensei_core.db import make_database
from sensei_core.models import Snapshot
from sensei_engine.embed import store
from sensei_engine.embed.cache import embed_documents
from sensei_engine.embed.provider import EmbeddingProvider, EmbeddingUsage
from sensei_engine.embed.voyage import VoyageClient
from sensei_engine.search.bm25 import RRF_K, Leg, LexHit, chunk_partition, keyword_search
from sensei_engine.search.dense import DenseHit, DenseRetriever
from sensei_engine.search.dense import Leg as DenseLeg
from sensei_evals.keyword import KeywordRetriever, analyze_index
from sensei_evals.retrieval import Hit, RepoRef, RetrieverContext
from sensei_evals.symbols import EVAL_DATABASE, Sessions, eval_database_url

GROUP = 1_000  # chunks per transaction, as the embeddings stage takes them
POOL = 60  # chunks each leg contributes to the fusion
MODES = ("dense", "hybrid")


async def embed_snapshot(
    sessions: Sessions,
    provider: EmbeddingProvider,
    snapshot_id: uuid.UUID,
    *,
    on_usage: Callable[[EmbeddingUsage], Awaitable[None]] | None = None,
) -> tuple[int, int, bool]:
    """Give every chunk of the snapshot its vector (cache first), then build the partition's
    HNSW index if it has none: ``(texts from the cache, texts embedded, index built)``."""
    cached = embedded = 0
    after: uuid.UUID | None = None
    while True:
        async with sessions() as db:
            snapshot = await db.get(Snapshot, snapshot_id)
            if snapshot is None:  # pragma: no cover - indexed just before
                msg = "the eval snapshot is gone"
                raise RuntimeError(msg)
            chunks = await store.pending(
                db, snapshot_id, generation=snapshot.chunk_generation, after=after, limit=GROUP
            )
            if not chunks:
                partition = await chunk_partition(db, snapshot.repo_id)
                break
            found = await embed_documents(
                db,
                provider,
                [(chunk.content_hash, chunk.text) for chunk in chunks],
                org_id=snapshot.org_id,
                on_usage=on_usage,
            )
            vectors = {chunk.id: found.vectors[chunk.content_hash] for chunk in chunks}
            await store.save(db, snapshot.repo_id, vectors)
        cached += found.hits
        embedded += found.embedded
        after = chunks[-1].id
    built = False
    if partition is not None:
        async with sessions() as db:
            built = await store.build_index(db, partition)
    return cached, embedded, built


def _hit(found: DenseHit | LexHit) -> Hit:
    return Hit(
        path=found.path,
        lines=(found.start_line, found.end_line),
        score=found.score,
        symbol=found.symbol_key,
    )


def fuse(rankings: Sequence[Sequence[tuple[uuid.UUID, Hit]]], k: int) -> list[Hit]:
    """Reciprocal rank fusion of chunk rankings: a chunk scores the sum of ``1 / (60 + rank)``
    over the rankings that hold it; best first (ties: the first ranking's order)."""
    scores: dict[uuid.UUID, float] = {}
    first: dict[uuid.UUID, Hit] = {}
    for ranking in rankings:
        for rank, (chunk_id, hit) in enumerate(ranking, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            first.setdefault(chunk_id, hit)
    ordered = sorted(scores, key=lambda chunk_id: -scores[chunk_id])[:k]
    return [
        Hit(
            path=first[c].path,
            lines=first[c].lines,
            score=round(scores[c], 6),
            symbol=first[c].symbol,
        )
        for c in ordered
    ]


class SemanticRetriever:
    """Dense search over one snapshot's chunks (``dense``), or fused with BM25 (``hybrid``).
    :meth:`prepare` indexes the checkout (through ``keyword``) and embeds it."""

    def __init__(
        self,
        sessions: Sessions,
        provider: EmbeddingProvider,
        keyword: KeywordRetriever,
        *,
        mode: str = "dense",
        analyze: Callable[[], Awaitable[None]] | None = None,
        close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if mode not in MODES:
            msg = f"mode must be one of {', '.join(MODES)}, not {mode!r}"
            raise ValueError(msg)
        self.name = mode
        self._sessions = sessions
        self._provider = provider
        self._dense = DenseRetriever(provider)
        self._keyword = keyword
        self._analyze = analyze
        self._close = close
        self._prepared = False
        self.tokens = 0  # what Voyage billed, documents and queries
        self.cached: int | None = None
        self.embedded: int | None = None
        self.embed_seconds: float | None = None

    async def _bill(self, usage: EmbeddingUsage) -> None:
        self.tokens += usage.tokens

    async def prepare(self, repo: RepoRef) -> None:
        """Index and embed the checkout, once (outside the searches' timing)."""
        if self._prepared:
            return
        await self._keyword.prepare(repo)
        found = self._keyword.snapshot
        if found is None:  # pragma: no cover - prepare set it or raised
            msg = "the keyword retriever has no snapshot"
            raise RuntimeError(msg)
        started = time.monotonic()
        self.cached, self.embedded, _ = await embed_snapshot(
            self._sessions, self._provider, found[1], on_usage=self._bill
        )
        if self._analyze is not None:
            await self._analyze()
        self.embed_seconds = time.monotonic() - started
        self._prepared = True

    async def search(self, query: str, repo: RepoRef, k: int) -> list[Hit]:
        await self.prepare(repo)
        found = self._keyword.snapshot
        if found is None:  # pragma: no cover - prepare set it or raised
            return []
        repo_id, snapshot_id = found
        pool = max(k, POOL)
        async with self._sessions() as db:
            if not db.in_transaction():  # a session of its own (a test's may be one already)
                # The legs read one snapshot of the database, so one generation of the chunks.
                await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            dense = await self._dense.search(
                db, repo_id=repo_id, snapshot_id=snapshot_id, query=query, k=pool,
                legs=(DenseLeg.CODE,), on_usage=self._bill,
            )  # fmt: skip
            if self.name == "dense":
                return [_hit(hit) for hit in dense[:k]]
            rankings: list[list[tuple[uuid.UUID, Hit]]] = [[(h.chunk_id, _hit(h)) for h in dense]]
            for leg in (Leg.CODE, Leg.PATH):
                lexical = await keyword_search(
                    db, repo_id=repo_id, snapshot_id=snapshot_id, query=query, k=pool, legs=(leg,)
                )
                rankings.append([(h.chunk_id, _hit(h)) for h in lexical])
        return fuse(rankings, k)

    def report(self) -> Mapping[str, object]:
        """What the run cost and found in the cache, for the eval's notes."""
        return {
            "tokens": self.tokens,
            "cached": self.cached,
            "embedded": self.embedded,
            "embed_seconds": None if self.embed_seconds is None else round(self.embed_seconds, 1),
        }

    async def aclose(self) -> None:
        await self._keyword.aclose()
        if self._close is not None:
            await self._close()


def checkout_semantic(context: RetrieverContext, mode: str) -> SemanticRetriever:
    """The ``dense`` and ``hybrid`` retrievers of ``sensei-eval run``: the checkout indexed into
    the eval database (``--opt db=<name>``, default :data:`EVAL_DATABASE`) and embedded with the
    deployment's Voyage model (its key from the settings)."""
    if context.repo.checkout is None:
        msg = f"the {mode} retriever needs a checkout of the repository (--checkout)"
        raise ValueError(msg)
    settings = get_settings()
    if not settings.voyage.configured:
        msg = f"the {mode} retriever needs a Voyage API key (SENSEI_VOYAGE_API_KEY)"
        raise ValueError(msg)
    name = context.options.get("db", EVAL_DATABASE)
    url = eval_database_url(settings.database.system_url.get_secret_value(), name)
    database = make_database(settings.database, url=url)
    owner_url = eval_database_url(settings.database.migrate_url.get_secret_value(), name)
    keyword = KeywordRetriever(database.session, checkout=context.repo.checkout)
    http = httpx.AsyncClient()

    async def close() -> None:
        await http.aclose()
        await database.dispose()

    return SemanticRetriever(
        database.session,
        VoyageClient(settings.voyage, http),
        keyword,
        mode=mode,
        analyze=lambda: analyze_index(owner_url),
        close=close,
    )
