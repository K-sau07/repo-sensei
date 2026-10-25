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

Scores are cosine similarities (1 - distance), higher is better. :func:`fuse` combines legs by
reciprocal rank (k = 60), as keyword search does.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.types import vector_literal
from sensei_engine.embed.provider import EmbeddingProvider, InputType, UsageSink
from sensei_engine.search.bm25 import PARTITIONS, RRF_K, Filters, searchable_partition

EF_SEARCH = 120
_SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")


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
    """``schema.chunks_pNN``: a partition of ``chunks``, schema-qualified (safe in SQL)."""
    schema, _, name = partition.rpartition(".")
    return _SCHEMA.fullmatch(schema) is not None and name in PARTITIONS


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
    if not _partition_name(partition):
        msg = f"not a partition of chunks: {partition!r}"
        raise ValueError(msg)
    conditions = [
        "c.repo_id = :repo_id",
        "c.snapshot_id = :snapshot_id",
        "c.view = :view",
        "c.embedding IS NOT NULL",
    ]
    params: dict[str, Any] = {
        "repo_id": repo_id,
        "snapshot_id": snapshot_id,
        "view": VIEWS[leg],
        "query": vector_literal(vector),
        "k": k,
    }
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
            "c.file_id IN (SELECT f.id FROM files f "
            "WHERE f.snapshot_id = :snapshot_id AND f.role = ANY(:roles))"
        )
        params["roles"] = list(filters.roles)
    where = " AND ".join(conditions)
    statement = text(
        "WITH nearest AS MATERIALIZED ("  # noqa: S608 - the names are a partition's and constants
        "SELECT c.id, c.kind, c.path, c.start_line, c.end_line, c.symbol_key, c.symbol_id, "
        "c.embedding <=> CAST(:query AS halfvec) AS distance "
        f"FROM {partition} c WHERE {where} "
        "ORDER BY c.embedding <=> CAST(:query AS halfvec) LIMIT :k"
        ") SELECT * FROM nearest ORDER BY distance, path, start_line"
    )
    return statement, params


async def tune(db: AsyncSession) -> None:
    """The HNSW settings searches use, for the rest of the transaction."""
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
    await db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))


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
    """The snapshot's ``k`` chunks nearest ``vector`` (a query's embedding) on one leg, nearest
    first (none while the snapshot isn't chunked). ``partition``:
    :func:`~sensei_engine.search.bm25.searchable_partition`'s, when the caller has it. Sets
    ``hnsw.*`` for the rest of the transaction (:func:`tune`)."""
    if not vector or k <= 0:
        return []
    partition = partition or await searchable_partition(db, repo_id, snapshot_id)
    if partition is None or not _partition_name(partition):
        return []
    statement, params = nearest_query(
        partition,
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        vector=vector,
        leg=leg,
        k=k,
        filters=filters,
    )
    await tune(db)
    rows = await db.execute(statement, params)
    return [
        DenseHit(
            chunk_id=row.id,
            kind=row.kind,
            path=row.path,
            start_line=row.start_line,
            end_line=row.end_line,
            symbol_key=row.symbol_key,
            symbol_id=row.symbol_id,
            score=round(1.0 - float(row.distance), 6),
            legs={leg.value: rank},
        )
        for rank, row in enumerate(rows, 1)
    ]


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
    snapshot's chunks on each leg, fused (one leg: its own ranking)."""

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
        results = {
            leg: await dense_search(
                db,
                repo_id=repo_id,
                snapshot_id=snapshot_id,
                vector=vector,
                leg=leg,
                k=k,
                filters=filters,
                partition=partition,
            )
            for leg in legs
        }
        if len(legs) == 1:
            return list(results[legs[0]])
        return fuse(results, weights=weights, k=k)
