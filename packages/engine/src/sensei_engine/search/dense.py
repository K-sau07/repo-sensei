"""Semantic search: the snapshot's chunks nearest a query's embedding, one leg per view.

- ``sem_code`` searches the code chunks' vectors;
- ``sem_summary`` the summary-view chunks' (none until summaries are chunked: it finds nothing).

The query is embedded as a query (asymmetric retrieval: :class:`InputType`) by the provider the
chunks were embedded with. Each leg reads the repository's partition directly (schema-qualified,
once the snapshot is chunked: :func:`~sensei_engine.search.bm25.searchable_partition`), in the
order of that partition's HNSW index (cosine distance on ``halfvec``), as keyword search does: the
partition holds every repository hashed there, so the snapshot, view and filters are checked as
the index is walked. With ``hnsw.iterative_scan = relaxed_order`` the walk goes on until ``k``
rows pass them (instead of returning what's left of ``ef_search`` candidates), and the rows come
back nearly in order: they are sorted again, materialized, by distance. ``hnsw.ef_search`` is
raised for the transaction. A partition without the index yet (its first vectors still loading)
is scanned exactly, in the same order; so is a snapshot that is a small part of its partition,
when the planner finds sorting its rows (through the snapshot's B-tree) cheaper than the walk.

Only the snapshot's live generation of chunks is read (``snapshots.chunk_generation``, checked
in the statement, as keyword search does): a re-chunk's rows are searched once its run makes
them live, and all at once. The legs of one search run in one statement, so they read the same
generation even if another goes live as they run.

Scores are cosine similarities (1 - distance), higher is better. :func:`fuse` combines legs by
reciprocal rank (k = 60), as keyword search does.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.types import vector_literal
from sensei_engine.embed.provider import EmbeddingProvider, InputType, UsageSink
from sensei_engine.search.bm25 import (
    PARTITIONS,
    RRF_K,
    Filters,
    chunk_partition,
    searchable_partition,
)
from sensei_engine.search.hybrid import ChunkRef

EF_SEARCH = 120


class Leg(enum.StrEnum):
    CODE = "sem_code"
    SUMMARY = "sem_summary"


VIEWS: dict[Leg, str] = {Leg.CODE: "code", Leg.SUMMARY: "summary"}
DEFAULT_WEIGHTS: dict[Leg, float] = {Leg.CODE: 1.0, Leg.SUMMARY: 0.7}


@dataclass(frozen=True, slots=True)
class DenseHit:
    chunk_id: uuid.UUID
    kind: str
    path: str
    start_line: int
    end_line: int
    symbol_key: str | None
    symbol_id: uuid.UUID | None
    score: float  # cosine similarity (or the fused score)
    legs: Mapping[str, int] = field(default_factory=dict[str, int])  # leg -> rank (1-based)


def _partition_name(partition: str) -> bool:
    """``public.chunks_pNN``: a partition of ``chunks``, schema-qualified (safe in SQL)."""
    return partition in PARTITIONS


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def nearest_query(
    partition: str,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    vector: Sequence[float],
    leg: Leg = Leg.CODE,
    k: int = 60,
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
) -> tuple[TextClause, dict[str, Any]]:
    """The statement :func:`dense_search` runs on ``partition`` (``schema.chunks_pNN``), and its
    parameters."""
    return legs_query(
        partition,
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        vector=vector,
        legs=(leg,),
        k=k,
        filters=filters,
    )


def legs_query(
    partition: str,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    vector: Sequence[float],
    legs: Sequence[Leg],
    k: int = 60,
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    generation: int | None = None,
) -> tuple[TextClause, dict[str, Any]]:
    """One statement for every leg on ``partition`` (``schema.chunks_pNN``): each leg's ``k``
    nearest chunks, tagged with the leg's position in ``legs``; and its parameters.
    ``generation``: the chunks to read, when the caller pinned them (else the live ones)."""
    if not _partition_name(partition):
        msg = f"not a partition of chunks: {partition!r}"
        raise ValueError(msg)
    conditions = [
        "c.repo_id = :repo_id",
        "c.snapshot_id = :snapshot_id",
        # The snapshot's live generation: the caller's, or read in the same statement.
        "c.generation = :generation"
        if generation is not None
        else "c.generation = (SELECT s.chunk_generation FROM public.snapshots s "
        "WHERE s.id = :snapshot_id AND s.repo_id = :repo_id)",
        "c.embedding IS NOT NULL",
    ]
    params: dict[str, Any] = {
        "repo_id": repo_id,
        "snapshot_id": snapshot_id,
        "query": vector_literal(vector),
        "k": k,
    }
    if generation is not None:
        params["generation"] = generation
    if filters.kinds:
        conditions.append("c.kind = ANY(:kinds)")
        params["kinds"] = list(filters.kinds)
    if filters.languages:
        conditions.append("c.lang = ANY(:languages)")
        params["languages"] = list(filters.languages)
    if filters.path_prefix:
        conditions.append("c.path LIKE :prefix ESCAPE '\\'")
        params["prefix"] = _like_prefix(filters.path_prefix)
    if filters.roles:
        conditions.append(
            "c.file_id IN (SELECT f.id FROM public.files f "
            "WHERE f.snapshot_id = :snapshot_id AND f.role = ANY(:roles))"
        )
        params["roles"] = list(filters.roles)
    where = " AND ".join(conditions)
    nearest: list[str] = []
    found: list[str] = []
    for position, leg in enumerate(legs):
        params[f"view_{position}"] = VIEWS[leg]
        nearest.append(
            f"leg_{position} AS MATERIALIZED ("  # noqa: S608 - a partition's name and constants
            "SELECT c.id, c.kind, c.path, c.start_line, c.end_line, c.symbol_key, c.symbol_id, "
            "c.embedding <=> CAST(:query AS halfvec) AS distance "
            f"FROM {partition} c WHERE {where} AND c.view = :view_{position} "
            "ORDER BY c.embedding <=> CAST(:query AS halfvec) LIMIT :k)"
        )
        found.append(f"SELECT {position} AS leg, * FROM leg_{position}")  # noqa: S608
    statement = text(
        f"WITH {', '.join(nearest)} "  # noqa: S608 - built above
        f"SELECT * FROM ({' UNION ALL '.join(found)}) found "
        "ORDER BY leg, distance, path, start_line"
    )
    return statement, params


async def tune(db: AsyncSession) -> None:
    """The HNSW settings searches use, for the rest of the transaction."""
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
    await db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))


async def search_legs(
    db: AsyncSession,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    vector: Sequence[float],
    legs: Sequence[Leg] = (Leg.CODE,),
    k: int = 60,
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    partition: str | None = None,
    generation: int | None = None,
) -> dict[Leg, list[DenseHit]]:
    """Each leg's ``k`` chunks nearest ``vector`` (a query's embedding), nearest first, read in
    one statement (nothing while the snapshot isn't chunked). ``partition`` and
    ``generation``: :func:`~sensei_engine.search.bm25.live_chunks`', when the caller has them.
    Sets ``hnsw.*`` for the rest of the transaction (:func:`tune`)."""
    legs = tuple(dict.fromkeys(legs))
    results: dict[Leg, list[DenseHit]] = {leg: [] for leg in legs}
    if not vector or k <= 0 or not legs:
        return results
    partition = partition or await searchable_partition(db, repo_id, snapshot_id)
    if partition is None or not _partition_name(partition):
        return results
    statement, params = legs_query(
        partition,
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        vector=vector,
        legs=legs,
        k=k,
        filters=filters,
        generation=generation,
    )
    await tune(db)
    for row in await db.execute(statement, params):
        leg: Leg = legs[int(row.leg)]
        hits = results[leg]
        hits.append(
            DenseHit(
                chunk_id=row.id,
                kind=row.kind,
                path=row.path,
                start_line=row.start_line,
                end_line=row.end_line,
                symbol_key=row.symbol_key,
                symbol_id=row.symbol_id,
                score=round(1.0 - float(row.distance), 6),
                legs={leg.value: len(hits) + 1},
            )
        )
    return results


async def dense_search(
    db: AsyncSession,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    vector: Sequence[float],
    leg: Leg = Leg.CODE,
    k: int = 60,
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    partition: str | None = None,
) -> list[DenseHit]:
    """The snapshot's ``k`` chunks nearest ``vector`` on one leg, nearest first (see
    :func:`search_legs`)."""
    found = await search_legs(
        db,
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        vector=vector,
        legs=(leg,),
        k=k,
        filters=filters,
        partition=partition,
    )
    return found[leg]


def fuse(
    results: Mapping[Leg, Sequence[DenseHit]],
    *,
    weights: Mapping[Leg, float] = DEFAULT_WEIGHTS,
    k: int | None = None,
) -> list[DenseHit]:
    """Reciprocal rank fusion: a chunk scores the sum of ``weight / (60 + rank)`` over the legs
    that found it; best first (ties keep the first leg's order)."""
    scores: dict[uuid.UUID, float] = {}
    first: dict[uuid.UUID, DenseHit] = {}
    ranks: dict[uuid.UUID, dict[str, int]] = {}
    for leg, hits in results.items():
        weight = weights.get(leg, 1.0)
        for rank, hit in enumerate(hits, 1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + weight / (RRF_K + rank)
            first.setdefault(hit.chunk_id, hit)
            ranks.setdefault(hit.chunk_id, {})[leg.value] = rank
    ordered = sorted(scores, key=lambda chunk_id: -scores[chunk_id])
    fused = [
        DenseHit(
            chunk_id=chunk_id,
            kind=first[chunk_id].kind,
            path=first[chunk_id].path,
            start_line=first[chunk_id].start_line,
            end_line=first[chunk_id].end_line,
            symbol_key=first[chunk_id].symbol_key,
            symbol_id=first[chunk_id].symbol_id,
            score=round(scores[chunk_id], 6),
            legs=ranks[chunk_id],
        )
        for chunk_id in ordered
    ]
    return fused if k is None else fused[:k]


class DenseRetriever:
    """Embeds a query with ``provider`` (the one the chunks were embedded with) and searches the
    snapshot's chunks on each leg (in one statement), fused (one leg: its own ranking)."""

    def __init__(self, provider: EmbeddingProvider) -> None:
        self._provider = provider

    async def embed_query(self, query: str, *, on_usage: UsageSink | None = None) -> list[float]:
        [vector] = await self._provider.embed([query], InputType.QUERY, on_usage=on_usage)
        return vector

    async def search(
        self,
        db: AsyncSession,
        *,
        repo_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        query: str,
        k: int = 60,
        legs: Sequence[Leg] = (Leg.CODE, Leg.SUMMARY),
        filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
        weights: Mapping[Leg, float] = DEFAULT_WEIGHTS,
        on_usage: UsageSink | None = None,
    ) -> list[DenseHit]:
        if not query.strip() or not legs:
            return []
        partition = await searchable_partition(db, repo_id, snapshot_id)
        if partition is None:
            return []  # not chunked: nothing to embed the query for
        vector = await self.embed_query(query, on_usage=on_usage)
        results = await search_legs(
            db,
            repo_id=repo_id,
            snapshot_id=snapshot_id,
            vector=vector,
            legs=legs,
            k=k,
            filters=filters,
            partition=partition,
        )
        if len(legs) == 1:
            return list(results[legs[0]])
        return fuse(results, weights=weights, k=k)


VIEW_LEGS: dict[str, Leg] = {view: leg for leg, view in VIEWS.items()}


class EmbeddedLegs:
    """Hybrid search's semantic legs (:class:`~sensei_engine.search.hybrid.DenseLegs`) on the
    snapshot's embeddings: the queries' texts embedded in one request (as queries, by the
    provider the chunks were embedded with, billed to ``on_usage``), then per text both views'
    nearest chunks in one statement, of the generation the search read."""

    def __init__(self, provider: EmbeddingProvider, *, on_usage: UsageSink | None = None) -> None:
        self._provider = provider
        self._on_usage = on_usage

    async def __call__(
        self,
        db: AsyncSession,
        *,
        repo_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        generation: int,
        queries: Sequence[tuple[str, str]],
        k: int,
        filters: Filters,
    ) -> list[list[ChunkRef]]:
        """One ranking per ``(text, view)`` query, in order (nothing for an empty text)."""
        for _, view in queries:
            if view not in VIEW_LEGS:
                msg = f"not a view of chunks: {view!r}"
                raise ValueError(msg)
        texts = list(dict.fromkeys(words for words, _ in queries if words.strip()))
        partition = await chunk_partition(db, repo_id) if texts and k > 0 else None
        if partition is None:
            return [[] for _ in queries]
        vectors = await self._provider.embed(texts, InputType.QUERY, on_usage=self._on_usage)
        found: dict[str, dict[Leg, list[DenseHit]]] = {}
        for words, vector in zip(texts, vectors, strict=True):
            views = tuple(dict.fromkeys(VIEW_LEGS[view] for t, view in queries if t == words))
            found[words] = await search_legs(
                db, repo_id=repo_id, snapshot_id=snapshot_id, vector=vector, legs=views, k=k,
                filters=filters, partition=partition, generation=generation,
            )  # fmt: skip
        return [
            [
                ChunkRef(hit.chunk_id, hit.symbol_key)
                for hit in found.get(words, {}).get(VIEW_LEGS[view], [])
            ]
            for words, view in queries
        ]
