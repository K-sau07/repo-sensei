"""Keyword search: BM25 (pg_textsearch) over a snapshot's chunks, one leg per text.

- ``lex_code`` ranks ``search_text``: the chunk's header and code;
- ``lex_path`` ranks ``path_text``: the path, the signature and the name.

Both texts were saved as index terms (:func:`~sensei_engine.chunk.identifiers.index_terms`:
``read_items`` is ``readitems read items``), and the query is made into terms the same way, so
the identifier itself is a rare term that ranks its definition first, and its words still match
prose. Every term is optional (pg_textsearch queries are OR); BM25 weighs rare terms up and
repeats down.

A snapshot is searched once it is chunked (``chunk_count`` set: the chunks stage sets it in the
transaction that writes them); before that, search finds nothing in it.

Each leg takes the best ``k`` chunks of the snapshot and view that its filters (kind, language,
path prefix, file role) keep, from the repository's partition, in the order of that partition's
BM25 index: scoring every row instead takes seconds on a large repository (langflow: 93,341
chunks, 9 s a query; the index, 10 to 40 ms, with the same rows). The partition is queried
directly, with its own index: pg_textsearch would score a query through the parent table with
the first partition's statistics (row-level security holds on every partition).

Scores are pg_textsearch's: negative, lower is better; a chunk matching no term scores 0 and is
left out. Hits carry the score negated (higher is better).

:func:`fuse` combines legs by reciprocal rank (k = 60), weighted per leg, as the design's hybrid
query does.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models.chunks import bm25_index, chunk_partitions
from sensei_engine.chunk.identifiers import index_terms

RRF_K = 60
POOL_SLACK = 20  # rows past k read from the index, so ties at the cut sort the same
MAX_QUERY_CHARS = 2000


class Leg(enum.StrEnum):
    CODE = "lex_code"
    PATH = "lex_path"


# The column each leg ranks (each partition has a BM25 index on it: bm25_index()).
COLUMNS: dict[Leg, str] = {Leg.CODE: "search_text", Leg.PATH: "path_text"}
PARTITIONS = frozenset(chunk_partitions())
_SCHEMA = re.compile(r"[a-z_][a-z0-9_]*")
DEFAULT_WEIGHTS: dict[Leg, float] = {Leg.CODE: 1.0, Leg.PATH: 0.8}


@dataclass(frozen=True, slots=True)
class Filters:
    kinds: Sequence[str] = ()  # chunk kinds: function, class, doc, ...
    languages: Sequence[str] = ()  # language ids
    roles: Sequence[str] = ()  # the file's role: api, ui, service, test, docs, ...
    path_prefix: str | None = None  # a directory: "backend/app/"


@dataclass(frozen=True, slots=True)
class LexHit:
    chunk_id: uuid.UUID
    kind: str
    path: str
    start_line: int
    end_line: int
    symbol_key: str | None
    symbol_id: uuid.UUID | None
    score: float  # higher is better
    legs: Mapping[str, int] = field(default_factory=dict[str, int])  # leg -> rank (1-based)


def bm25_query(query: str) -> str:
    """The query as the index's terms: each identifier joined, with its words."""
    return index_terms(query[:MAX_QUERY_CHARS])


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _partition_name(partition: str) -> bool:
    """``schema.chunks_pNN``: a partition of ``chunks``, schema-qualified (safe in SQL)."""
    schema, _, name = partition.rpartition(".")
    return _SCHEMA.fullmatch(schema) is not None and name in PARTITIONS


async def chunk_partition(db: AsyncSession, repo_id: uuid.UUID) -> str | None:
    """The partition holding the repository's chunks, schema-qualified (``public.chunks_p07``:
    the same name elsewhere on the search path can't stand in for it); None if the session sees
    none of the repository's chunks."""
    found = await db.scalar(
        text(
            "SELECT n.nspname || '.' || p.relname FROM chunks c "
            "JOIN pg_class p ON p.oid = c.tableoid "
            "JOIN pg_namespace n ON n.oid = p.relnamespace "
            "WHERE c.repo_id = :repo_id LIMIT 1"
        ),
        {"repo_id": repo_id},
    )
    name = str(found) if found is not None else None
    return name if name is not None and _partition_name(name) else None


async def searchable_partition(
    db: AsyncSession, repo_id: uuid.UUID, snapshot_id: uuid.UUID
) -> str | None:
    """The repository's partition, if the snapshot is chunked (None otherwise: not chunked
    yet, being chunked for the first time, or not the session's)."""
    ready = await db.scalar(
        text(
            "SELECT chunk_count IS NOT NULL FROM snapshots "
            "WHERE id = :snapshot_id AND repo_id = :repo_id"
        ),
        {"snapshot_id": snapshot_id, "repo_id": repo_id},
    )
    return await chunk_partition(db, repo_id) if ready else None


async def bm25_search(
    db: AsyncSession,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    query: str,
    leg: Leg = Leg.CODE,
    k: int = 60,
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    view: str = "code",
    partition: str | None = None,
) -> list[LexHit]:
    """The snapshot's ``k`` best chunks for ``query`` on one leg, best first (none if the
    snapshot isn't chunked). ``partition``: :func:`searchable_partition`'s, when the caller has
    it."""
    terms = bm25_query(query)
    if not terms.strip():
        return []
    partition = partition or await searchable_partition(db, repo_id, snapshot_id)
    if partition is None or not _partition_name(partition):
        return []
    column = COLUMNS[leg]
    index = bm25_index(partition, column)
    conditions = ["c.repo_id = :repo_id", "c.snapshot_id = :snapshot_id", "c.view = :view"]
    params: dict[str, Any] = {
        "repo_id": repo_id,
        "snapshot_id": snapshot_id,
        "view": view,
        "query": terms,
        "k": k,
        "pool": k + POOL_SLACK,
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
    # The partition's BM25 index walks the rows in score order, the filters checked as it goes
    # (pg_textsearch widens its top-k by the filters' selectivity); a few rows past ``k`` are
    # taken, so that ties at the cut sort by path and line, the same on every run.
    score = f"c.{column} <@> to_bm25query(:query, '{index}')"
    statement = text(
        "SELECT * FROM ("  # noqa: S608 - the names are a partition's and constants
        "SELECT c.id, c.kind, c.path, c.start_line, c.end_line, c.symbol_key, c.symbol_id, "
        f"{score} AS score FROM {partition} c WHERE {where} ORDER BY {score} LIMIT :pool"
        ") scored WHERE score < 0 "  # 0: no query term in the chunk
        "ORDER BY score, path, start_line LIMIT :k"
    )
    rows = await db.execute(statement, params)
    return [
        LexHit(
            chunk_id=row.id,
            kind=row.kind,
            path=row.path,
            start_line=row.start_line,
            end_line=row.end_line,
            symbol_key=row.symbol_key,
            symbol_id=row.symbol_id,
            score=round(-float(row.score), 6),
            legs={leg.value: rank},
        )
        for rank, row in enumerate(rows, 1)
    ]


def fuse(
    results: Mapping[Leg, Sequence[LexHit]],
    *,
    weights: Mapping[Leg, float] = DEFAULT_WEIGHTS,
    k: int | None = None,
) -> list[LexHit]:
    """Reciprocal rank fusion: a chunk scores the sum of ``weight / (60 + rank)`` over the legs
    that found it; best first (ties keep the first leg's order)."""
    scores: dict[uuid.UUID, float] = {}
    first: dict[uuid.UUID, LexHit] = {}
    ranks: dict[uuid.UUID, dict[str, int]] = {}
    for leg, hits in results.items():
        weight = weights.get(leg, 1.0)
        for rank, hit in enumerate(hits, 1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + weight / (RRF_K + rank)
            first.setdefault(hit.chunk_id, hit)
            ranks.setdefault(hit.chunk_id, {})[leg.value] = rank
    ordered = sorted(scores, key=lambda chunk_id: -scores[chunk_id])
    fused = [
        LexHit(
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


async def keyword_search(
    db: AsyncSession,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    query: str,
    k: int = 60,
    legs: Sequence[Leg] = (Leg.CODE, Leg.PATH),
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    weights: Mapping[Leg, float] = DEFAULT_WEIGHTS,
) -> list[LexHit]:
    """Each leg's ``k`` best, fused (one leg: its own ranking)."""
    partition = await searchable_partition(db, repo_id, snapshot_id)
    if partition is None:
        return []
    results = {
        leg: await bm25_search(
            db,
            repo_id=repo_id,
            snapshot_id=snapshot_id,
            query=query,
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
