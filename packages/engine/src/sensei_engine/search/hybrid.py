"""Hybrid retrieval (2.9): a search plan's legs (:mod:`sensei_engine.search.plan`) run and fused.

Each sub-query of the plan is one ranking:

* ``lex_code``, ``lex_path`` and ``lex_summary``: BM25 (:mod:`sensei_engine.search.bm25`) on the
  code view's ``search_text`` and ``path_text`` and the summary view's ``search_text``, every
  ranking its own ``ORDER BY … LIMIT`` subquery (``MATERIALIZED``) of one statement;
* ``sym``: :func:`~sensei_engine.symbols.lookup.find_symbols` (exact, prefix, qualified,
  contains, then trigram typos), each symbol at its chunk;
* ``grep``: exact text or a pattern, through a :class:`GrepLegs` when the caller has the
  snapshot's files (each matched line at the chunk around it), the path prefix searched as
  ripgrep globs (:func:`path_globs`) so the leg's lines come from inside it;
* ``sem_code`` and ``sem_summary``: nearest embeddings, through a :class:`DenseLegs` once 2.7 has
  embedded the snapshot; without one they are reported pending.

Rankings are fused by reciprocal rank, ``Σ w / (k + rank)`` with ``k = 60``; a ranking's weight
is its leg's times its origin's (the query as asked, the model's rewrites, guessed names, routes,
paths: :class:`Weights`, from the settings). Fusion is per **symbol**: a definition's chunks
(the parts of a long function) are one result, at its best rank in each ranking, with a small
bonus for each further chunk of it that was found (a fragment never outranks its function on
its own). A chunk outside any definition (a Markdown section, a config key) is its own result.

Every leg applies the request's filters (kinds, languages, roles, a path prefix) before it ranks:
BM25 and the embeddings in their statements, symbols and grep where their places are located
(:func:`locate_all`), so a filtered search ranks only what it can show. The grep legs run at
once (ripgrep caps how many run), then every symbol and grep place is located in one query.

The fused results are then read in one query (name, kind, language, the file's role) and each
gets its layer (:mod:`sensei_engine.search.layers`). The plan's excluded layers ("not tests")
are dropped, and results in the layers the query names are lifted by ``layer_boost``; then the
best :data:`MAX_RESULTS` are kept.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import Integer, String, Text, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import SearchSettings
from sensei_core.models import CodeChunk, CodeNode, RepoFile
from sensei_engine.search.bm25 import (
    RRF_K,
    Filters,
    LexQuery,
    lexical_search,
    live_chunks,
)
from sensei_engine.search.bm25 import Leg as LexLeg
from sensei_engine.search.grep import (
    MAX_LIMIT,
    ExactSearch,
    GrepHit,
    GrepQuery,
    InvalidQueryError,
)
from sensei_engine.search.layers import lane_of, layer_of
from sensei_engine.search.plan import Leg, Origin, SearchPlan, SubQuery
from sensei_engine.search.route import Lane, Layer
from sensei_engine.symbols.lookup import find_symbols

LEG_DEPTH = 50  # each ranking's depth, at least (more when more results are asked for)
MAX_RESULTS = 200  # fused results read and tagged

LEXICAL: Mapping[Leg, LexQuery] = {
    Leg.LEX_CODE: LexQuery("", LexLeg.CODE, "code"),
    Leg.LEX_PATH: LexQuery("", LexLeg.PATH, "code"),
    Leg.LEX_SUMMARY: LexQuery("", LexLeg.CODE, "summary"),
}
SEMANTIC: Mapping[Leg, str] = {Leg.SEM_CODE: "code", Leg.SEM_SUMMARY: "summary"}


@dataclass(frozen=True, slots=True)
class Weights:
    """Fusion weights: a ranking weighs its leg's weight times its origin's (0 leaves it out).
    The defaults are :class:`~sensei_core.config.SearchSettings`'."""

    legs: Mapping[Leg, float] = field(
        default_factory=lambda: {
            Leg.SEM_CODE: 2.4,
            Leg.SEM_SUMMARY: 0.7,
            Leg.LEX_CODE: 0.6,
            Leg.LEX_PATH: 0.8,
            Leg.LEX_SUMMARY: 0.4,
            Leg.SYM: 0.6,
            Leg.GREP: 1.0,
        }
    )
    origins: Mapping[Origin, float] = field(
        default_factory=lambda: {
            Origin.ORIGINAL: 1.0,
            Origin.STRUCTURAL: 0.7,
            Origin.BEHAVIOURAL: 0.7,
            Origin.IDENTIFIER: 0.8,
            Origin.ROUTE: 0.8,
            Origin.PATH: 0.8,
        }
    )
    rrf_k: int = RRF_K
    # Each further chunk of a result found adds this share of a first place's score (at most
    # ``max_extra`` of them).
    multi_hit: float = 0.1
    max_extra: int = 3
    layer_boost: float = 1.0  # results in a layer the query names

    def of(self, sub: SubQuery) -> float:
        return self.legs.get(sub.leg, 0.0) * self.origins.get(sub.origin, 0.0)

    @classmethod
    def from_settings(cls, settings: SearchSettings) -> Weights:
        return cls(
            legs={leg: getattr(settings, leg.value) for leg in Leg},
            origins={origin: getattr(settings, f"origin_{origin.value}") for origin in Origin},
            rrf_k=settings.rrf_k,
            multi_hit=settings.multi_hit,
            layer_boost=settings.layer_boost,
        )


@dataclass(frozen=True, slots=True)
class ChunkRef:
    """A chunk a ranking found."""

    chunk_id: uuid.UUID
    symbol_key: str | None
    # The definition the leg found in a chunk that isn't one (small definitions merged): the
    # result is still the chunk, reported as that definition.
    focus: str | None = None

    @property
    def unit(self) -> str:
        """The result it counts toward: its definition, or the chunk alone."""
        return self.symbol_key if self.symbol_key is not None else f"chunk:{self.chunk_id}"


@dataclass(frozen=True, slots=True)
class Ranking:
    sub: SubQuery
    found: Sequence[ChunkRef]  # best first
    seconds: float = 0.0


class GrepLegs(Protocol):
    """Runs a ``grep`` sub-query over the snapshot's files: the matched lines, as (path, line),
    in the order they rank."""

    async def __call__(
        self, sub: SubQuery, limit: int, filters: Filters
    ) -> Sequence[tuple[str, int]]: ...


_GLOB_SPECIAL = frozenset("\\*?[]{}!")


def path_globs(prefix: str | None) -> tuple[str, ...]:
    """ripgrep globs for the paths that start with ``prefix`` (``src/ap`` takes in ``src/app/``
    and ``src/api.ts``): literal (``[org]`` isn't a character class), anchored at the root. As the
    search page's exact mode builds them."""
    stripped = (prefix or "").strip().lstrip("/")
    if not stripped:
        return ()
    literal = "".join(f"\\{ch}" if ch in _GLOB_SPECIAL else ch for ch in stripped)
    return (f"/{literal}*", f"/{literal}*/**")


class WorktreeGrep:
    """The grep legs on a snapshot's files (an unpacked snapshot or a checkout), with ripgrep:
    files with more matches first, then by path, each file's lines in order; ``files``, the
    paths it may report (what discovery kept)."""

    def __init__(
        self,
        root: Path,
        *,
        search: ExactSearch | None = None,
        files: Container[str] | None = None,
    ) -> None:
        self._root = root
        self._search = search or ExactSearch()
        self._files = files

    async def __call__(
        self,
        sub: SubQuery,
        limit: int,
        filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    ) -> list[tuple[str, int]]:
        try:
            query = GrepQuery(
                sub.text,
                regex=sub.regex,
                globs=path_globs(filters.path_prefix),
                limit=min(limit * 4, MAX_LIMIT),
            )
            result = await self._search.search(self._root, query, files=self._files)
        except InvalidQueryError:
            return []
        return grep_places(result.hits, limit)


def grep_places(hits: Iterable[GrepHit], limit: int) -> list[tuple[str, int]]:
    """A grep leg's ranking of matched lines: files with more matches first, then by path,
    each file's lines in order (at most ``4 * limit``: a definition often holds several)."""
    lines: dict[str, list[int]] = {}
    for hit in hits:
        lines.setdefault(hit.path, []).append(hit.line)
    ordered = sorted(lines, key=lambda path: (-len(lines[path]), path))
    return [(path, line) for path in ordered for line in lines[path]][: limit * 4]


class DenseLegs(Protocol):
    """Runs ``sem_code`` and ``sem_summary`` sub-queries (2.7): per query (text and view), the
    nearest chunks of the snapshot's ``generation`` (every leg of a search reads the one), best
    first."""

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
    ) -> Sequence[Sequence[ChunkRef]]: ...


@dataclass(frozen=True, slots=True)
class Fused:
    unit: str
    chunk_id: uuid.UUID  # the chunk that contributed most: the result's snippet
    score: float
    legs: Mapping[str, int]  # leg -> its best rank of the result (1-based)
    chunks: int  # how many of its chunks were found
    first: int  # where it was first found (ties keep this order)
    focus: str | None = None  # the definition found in it (see :class:`ChunkRef`)


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: uuid.UUID
    symbol_key: str | None
    symbol_id: uuid.UUID | None
    name: str | None  # the definition's (None: a chunk outside any)
    kind: str  # the definition's kind, or the chunk's
    path: str
    language: str | None
    layer: Layer | None
    lane: Lane
    start_line: int  # the definition's span (cut into parts, or one of several), else the chunk's
    end_line: int
    chunk_start: int  # the chunk's own lines (the snippet)
    chunk_end: int
    score: float
    legs: Mapping[str, int]
    chunks: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: list[SearchHit]
    rankings: list[Ranking]
    pending: tuple[Leg, ...] = ()  # legs of the plan that couldn't run yet (no embeddings)


def fuse(rankings: Sequence[Ranking], weights: Weights) -> list[Fused]:
    """Reciprocal rank fusion per result (see the module); best first."""
    k = weights.rrf_k
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    best_ranks: dict[str, dict[str, int]] = {}
    chunks: dict[str, dict[uuid.UUID, float]] = {}  # result -> chunk -> what it contributed
    focuses: dict[str, dict[str, float]] = {}  # result -> definition found in it -> the same
    for ranking in rankings:
        weight = weights.of(ranking.sub)
        if weight <= 0:
            continue
        leg = ranking.sub.leg.value
        rank = 0
        seen: set[str] = set()
        for ref in ranking.found:
            unit = ref.unit
            contributed = chunks.setdefault(unit, {})
            if unit in seen:  # a further chunk of a result this ranking already placed
                contributed.setdefault(ref.chunk_id, 0.0)
                continue
            seen.add(unit)
            rank += 1
            share = weight / (k + rank)
            scores[unit] = scores.get(unit, 0.0) + share
            order.setdefault(unit, len(order))
            contributed[ref.chunk_id] = contributed.get(ref.chunk_id, 0.0) + share
            if ref.focus is not None:
                found_in = focuses.setdefault(unit, {})
                found_in[ref.focus] = found_in.get(ref.focus, 0.0) + share
            ranks = best_ranks.setdefault(unit, {})
            ranks[leg] = min(ranks.get(leg, rank), rank)
    first_place = 1 / (k + 1)
    fused: list[Fused] = []
    for unit, score in scores.items():
        found = chunks[unit]
        extra = min(len(found) - 1, weights.max_extra)
        chunk_id = max(found, key=lambda chunk: found[chunk])  # the first of equals
        found_in = focuses.get(unit, {})
        fused.append(
            Fused(
                unit=unit,
                chunk_id=chunk_id,
                score=score + weights.multi_hit * extra * first_place,
                legs=best_ranks[unit],
                chunks=len(found),
                first=order[unit],
                focus=max(found_in, key=lambda key: found_in[key]) if found_in else None,
            )
        )
    fused.sort(key=lambda result: (-result.score, result.first))
    return fused


_LOCATE = text(
    "SELECT q.i, c.id, c.symbol_key, "
    "CASE WHEN c.symbol_key IS NULL THEN coalesce(q.key, n.symbol_key) END AS focus "
    "FROM unnest(:paths, :lines, :keys) "
    "WITH ORDINALITY AS q(path, line, key, i) "
    "JOIN public.files f ON f.snapshot_id = :snapshot_id AND f.path = q.path "
    # The request's filters, as the other legs apply them.
    "AND (cardinality(:languages) = 0 OR f.language = ANY(:languages)) "
    "AND (cardinality(:roles) = 0 OR f.role = ANY(:roles)) "
    "AND (:prefix = '' OR f.path LIKE :prefix ESCAPE '\\') "
    "CROSS JOIN LATERAL ("
    "SELECT c.id, c.symbol_key FROM public.chunks c "
    "WHERE c.repo_id = :repo_id AND c.file_id = f.id AND c.view = 'code' "
    "AND c.generation = :generation "
    "AND (cardinality(:kinds) = 0 OR c.kind = ANY(:kinds)) "
    "AND (c.symbol_key = q.key OR (c.start_line <= q.line AND q.line <= c.end_line)) "
    # The definition by its key (its first part), else the innermost chunk around the line.
    "ORDER BY c.symbol_key = q.key DESC NULLS LAST, "
    "CASE WHEN c.symbol_key = q.key THEN c.start_line END, "
    "c.end_line - c.start_line, c.start_line LIMIT 1"
    ") c "
    # In a chunk of several definitions, a line's is the innermost around it.
    "LEFT JOIN LATERAL (SELECT n.symbol_key FROM public.nodes n "
    "WHERE q.key IS NULL AND c.symbol_key IS NULL AND n.file_id = f.id "
    "AND n.start_line <= q.line AND q.line <= n.end_line "
    "ORDER BY n.end_line - n.start_line, n.start_line DESC LIMIT 1"
    ") n ON true ORDER BY q.i"
).bindparams(
    bindparam("paths", type_=ARRAY(Text)),
    bindparam("lines", type_=ARRAY(Integer)),
    bindparam("keys", type_=ARRAY(String)),
    bindparam("languages", type_=ARRAY(String)),
    bindparam("roles", type_=ARRAY(String)),
    bindparam("kinds", type_=ARRAY(String)),
)

Place = tuple[str, int, str | None]  # path, line, and the symbol's key if the place is one


async def locate(
    db: AsyncSession,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    generation: int,
    places: Sequence[Place],
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
) -> list[ChunkRef]:
    """The chunk of each place (path, line, and the symbol's key if it is one), in order, with
    the definition at the place when the chunk isn't one; a place with no chunk (a file that
    wasn't chunked) or outside ``filters`` is left out."""
    [found] = await locate_all(
        db,
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        generation=generation,
        groups=[places],
        filters=filters,
    )
    return found


async def locate_all(
    db: AsyncSession,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    generation: int,
    groups: Sequence[Sequence[Place]],
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
) -> list[list[ChunkRef]]:
    """:func:`locate` for several rankings' places at once (one query), each group's in order."""
    places = [place for group in groups for place in group]
    owner = [n for n, group in enumerate(groups) for _ in group]
    found: list[list[ChunkRef]] = [[] for _ in groups]
    if not places:
        return found
    rows = await db.execute(
        _LOCATE,
        {
            "paths": [path for path, _, _ in places],
            "lines": [line for _, line, _ in places],
            "keys": [key for _, _, key in places],
            "snapshot_id": snapshot_id,
            "repo_id": repo_id,
            "generation": generation,
            "languages": list(filters.languages),
            "roles": list(filters.roles),
            "kinds": list(filters.kinds),
            "prefix": _like_prefix(filters.path_prefix) if filters.path_prefix else "",
        },
    )
    for row in rows:  # in place order
        found[owner[int(row.i) - 1]].append(ChunkRef(row.id, row.symbol_key, row.focus))
    return found


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


class HybridRetriever:
    """Runs a plan on one snapshot (see the module). ``grep`` and ``dense`` are optional: a plan's
    grep legs need the snapshot's files, its semantic legs its embeddings."""

    def __init__(
        self,
        *,
        weights: Weights | None = None,
        grep: GrepLegs | None = None,
        dense: DenseLegs | None = None,
        depth: int = LEG_DEPTH,
    ) -> None:
        self.weights = weights or Weights()
        self._grep = grep
        self._dense = dense
        self._depth = depth

    async def rankings(
        self,
        db: AsyncSession,
        plan: SearchPlan,
        *,
        repo_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        k: int = 20,
        filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    ) -> tuple[list[Ranking], tuple[Leg, ...]]:
        """Every ranking of the plan, in plan order, and the legs that couldn't run. Every leg
        reads the snapshot's live generation as the search found it (:func:`live_chunks`)."""
        depth = max(k, self._depth)
        found: dict[int, Ranking] = {}
        pending: list[Leg] = []
        live = await live_chunks(db, repo_id, snapshot_id)
        if live is None:
            return [], ()
        lexical = [(n, sub) for n, sub in enumerate(plan.legs) if sub.leg in LEXICAL]
        if lexical:
            started = time.monotonic()
            results = await lexical_search(
                db,
                repo_id=repo_id,
                snapshot_id=snapshot_id,
                queries=[
                    LexQuery(sub.text, LEXICAL[sub.leg].leg, LEXICAL[sub.leg].view)
                    for _, sub in lexical
                ],
                k=depth,
                filters=filters,
                partition=live.partition,
                generation=live.generation,
            )
            seconds = (time.monotonic() - started) / len(lexical)
            for (n, sub), hits in zip(lexical, results, strict=True):
                refs = [ChunkRef(hit.chunk_id, hit.symbol_key) for hit in hits]
                found[n] = Ranking(sub, refs, seconds)
        semantic = [(n, sub) for n, sub in enumerate(plan.legs) if sub.leg in SEMANTIC]
        if semantic and self._dense is None:
            pending.extend(dict.fromkeys(sub.leg for _, sub in semantic))
        elif semantic and self._dense is not None:
            started = time.monotonic()
            results = await self._dense(
                db,
                repo_id=repo_id,
                snapshot_id=snapshot_id,
                generation=live.generation,
                queries=[(sub.text, SEMANTIC[sub.leg]) for _, sub in semantic],
                k=depth,
                filters=filters,
            )
            seconds = (time.monotonic() - started) / len(semantic)
            for (n, sub), refs in zip(semantic, results, strict=True):
                found[n] = Ranking(sub, list(refs), seconds)
        started = time.monotonic()
        located: list[tuple[int, SubQuery]] = []
        groups: list[Sequence[Place]] = []
        greps = [(n, sub) for n, sub in enumerate(plan.legs) if sub.leg is Leg.GREP]
        if greps and self._grep is not None:
            grep = self._grep
            lines = await asyncio.gather(*(grep(sub, depth, filters) for _, sub in greps))
            for (n, sub), found_lines in zip(greps, lines, strict=True):
                located.append((n, sub))
                groups.append([(path, line, None) for path, line in found_lines])
        for n, sub in enumerate(plan.legs):
            if sub.leg is Leg.SYM:
                symbols = await find_symbols(db, snapshot_id, sub.text, limit=depth)
                located.append((n, sub))
                groups.append([(hit.path, hit.start_line, hit.symbol_key) for hit in symbols])
        if located:
            refs = await locate_all(
                db,
                repo_id=repo_id,
                snapshot_id=snapshot_id,
                generation=live.generation,
                groups=groups,
                filters=filters,
            )
            seconds = (time.monotonic() - started) / len(located)
            for (n, sub), ranked in zip(located, refs, strict=True):
                found[n] = Ranking(sub, ranked, seconds)
        return [found[n] for n in sorted(found)], tuple(pending)

    async def search(
        self,
        db: AsyncSession,
        plan: SearchPlan,
        *,
        repo_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        k: int = 20,
        filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    ) -> SearchResult:
        """The plan's ``k`` best results, best first."""
        rankings, pending = await self.rankings(
            db, plan, repo_id=repo_id, snapshot_id=snapshot_id, k=k, filters=filters
        )
        hits = await self.results(
            db, plan, rankings, repo_id=repo_id, snapshot_id=snapshot_id, filters=filters
        )
        return SearchResult(hits[:k], rankings, pending)

    async def results(
        self,
        db: AsyncSession,
        plan: SearchPlan,
        rankings: Sequence[Ranking],
        *,
        repo_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
        weights: Weights | None = None,
    ) -> list[SearchHit]:
        """The rankings fused, read, tagged and filtered by layer, then the best
        :data:`MAX_RESULTS` (cut after the filters, so a filtered search keeps as many)."""
        fused = fuse(rankings, weights or self.weights)
        details = await read_chunks(db, repo_id, [result.chunk_id for result in fused])
        focus = await read_symbols(db, snapshot_id, [r.focus for r in fused if r.focus])
        hits = arrange(
            plan, fused, details, weights or self.weights, filters=filters, symbols=focus
        )
        return hits[:MAX_RESULTS]


@dataclass(frozen=True, slots=True)
class ChunkDetails:
    chunk_id: uuid.UUID
    symbol_key: str | None
    symbol_id: uuid.UUID | None
    kind: str
    path: str
    start_line: int
    end_line: int
    parts: int | None
    language: str | None
    role: str | None
    name: str | None
    node_kind: str | None
    node_start: int | None
    node_end: int | None


async def read_chunks(
    db: AsyncSession, repo_id: uuid.UUID, chunk_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ChunkDetails]:
    """The chunks, with their file's language and role and their definition's name and span."""
    if not chunk_ids:
        return {}
    rows = await db.execute(
        select(
            CodeChunk.id,
            CodeChunk.symbol_key,
            CodeChunk.symbol_id,
            CodeChunk.kind,
            CodeChunk.path,
            CodeChunk.start_line,
            CodeChunk.end_line,
            CodeChunk.parts,
            RepoFile.language,
            RepoFile.role,
            CodeNode.name,
            CodeNode.kind,
            CodeNode.start_line,
            CodeNode.end_line,
        )
        .join(RepoFile, RepoFile.id == CodeChunk.file_id)
        .outerjoin(CodeNode, CodeNode.id == CodeChunk.symbol_id)
        .where(CodeChunk.repo_id == repo_id, CodeChunk.id.in_(list(chunk_ids)))
    )
    return {row[0]: ChunkDetails(*row) for row in rows}


@dataclass(frozen=True, slots=True)
class SymbolDetails:
    symbol_id: uuid.UUID
    symbol_key: str
    name: str
    kind: str
    start_line: int
    end_line: int


async def read_symbols(
    db: AsyncSession, snapshot_id: uuid.UUID, keys: Sequence[str]
) -> dict[str, SymbolDetails]:
    """The snapshot's definitions by key."""
    if not keys:
        return {}
    rows = await db.execute(
        select(
            CodeNode.id,
            CodeNode.symbol_key,
            CodeNode.name,
            CodeNode.kind,
            CodeNode.start_line,
            CodeNode.end_line,
        ).where(CodeNode.snapshot_id == snapshot_id, CodeNode.symbol_key.in_(sorted(set(keys))))
    )
    return {row[1]: SymbolDetails(*row) for row in rows}


def _kept(chunk: ChunkDetails, filters: Filters) -> bool:
    """The request's filters, on every leg's results (the lexical legs apply them as they read;
    symbols and grep look in the whole snapshot)."""
    return (
        (not filters.kinds or chunk.kind in filters.kinds)
        and (not filters.languages or chunk.language in filters.languages)
        and (not filters.roles or chunk.role in filters.roles)
        and (not filters.path_prefix or chunk.path.startswith(filters.path_prefix))
    )


def arrange(
    plan: SearchPlan,
    fused: Sequence[Fused],
    details: Mapping[uuid.UUID, ChunkDetails],
    weights: Weights,
    *,
    filters: Filters = Filters(),  # noqa: B008 - frozen, never mutated
    symbols: Mapping[str, SymbolDetails] | None = None,
) -> list[SearchHit]:
    """Fused results as hits: tagged, the excluded layers dropped, the named ones lifted. A
    result found as a definition inside a chunk of several (``symbols``: by key) is reported as
    that definition, the chunk its snippet."""
    excluded = set(plan.excluded_layers)
    named = set(plan.layers)
    hits: list[tuple[float, int, SearchHit]] = []
    for result in fused:
        chunk = details.get(result.chunk_id)
        if chunk is None:
            continue
        focus = (symbols or {}).get(result.focus or "") if chunk.symbol_key is None else None
        kind = focus.kind if focus else chunk.node_kind or chunk.kind
        layer = layer_of(chunk.role, kind, chunk.path)
        if layer in excluded or not _kept(chunk, filters):
            continue
        whole = chunk.parts is not None and chunk.parts > 1 and chunk.node_start is not None
        start, end = (
            (focus.start_line, focus.end_line)
            if focus
            else (chunk.node_start, chunk.node_end)
            if whole and chunk.node_start and chunk.node_end
            else (chunk.start_line, chunk.end_line)
        )
        score = result.score * (weights.layer_boost if layer in named else 1.0)
        hit = SearchHit(
            chunk_id=chunk.chunk_id,
            symbol_key=focus.symbol_key if focus else chunk.symbol_key,
            symbol_id=focus.symbol_id if focus else chunk.symbol_id,
            name=focus.name if focus else chunk.name,
            kind=kind,
            path=chunk.path,
            language=chunk.language,
            layer=layer,
            lane=lane_of(layer),
            start_line=start,
            end_line=end,
            chunk_start=chunk.start_line,
            chunk_end=chunk.end_line,
            score=round(score, 6),
            legs=result.legs,
            chunks=result.chunks,
        )
        hits.append((-score, result.first, hit))
    hits.sort(key=lambda entry: (entry[0], entry[1]))
    return [hit for _, _, hit in hits]
