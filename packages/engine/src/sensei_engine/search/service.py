"""Search as the search box and the agent's ``search`` tool ask for it (2.13): a query routed,
planned, run on every leg it can (:mod:`sensei_engine.search.hybrid`), reranked, and grouped by
swimlane.

1. **Route** (:class:`~sensei_engine.search.route.QueryRouter`): the rules, and the model for a
   query they can't decide (its answers cached per org; its usage billed through ``on_llm_usage``).
2. **Plan** (:func:`~sensei_engine.search.plan.plan`), narrowed by mode: ``keyword`` leaves the
   semantic legs out, ``semantic`` runs only them (:class:`SemanticNotReadyError` while the
   snapshot has no embeddings), ``auto`` runs what the plan says. Semantic legs that can't run
   yet make the answer partial (``semantic_pending``).
3. **Retrieve** and fuse: every result the legs found, filtered (languages, a path prefix, the
   layers the query excludes) and tagged with its layer and lane.
4. **Rerank** the best :data:`RERANK_CANDIDATES` with the cross-encoder, within the interactive
   budget (:class:`~sensei_engine.search.rerank.Reranker`); unconfigured, failing or too slow,
   the fused order stands. Scores are then 0-1 and comparable within the answer: the
   cross-encoder's relevance for what it ranked, the fused score below them for the rest.
5. **Group** by lane, each lane best first. ``why`` names the legs that found a result,
   strongest first (``keyword``: BM25 and exact text; ``symbol``: names; ``semantic``).

An :class:`Answer` holds every result (not a page) so a caller can page any lane; snippets are
cut for the results shown (:func:`snippets`): at most :data:`SNIPPET_LINES` lines of the result,
from its first line with a query term, the terms highlighted in UTF-16 columns.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from posixpath import basename

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.logging import get_logger
from sensei_core.models import CodeChunk
from sensei_engine.graph.snapshot import GraphSnapshot, neighbourhood
from sensei_engine.ingest.classify import text_lines
from sensei_engine.llm.anthropic_client import LLMUsageSink
from sensei_engine.search.bm25 import Filters
from sensei_engine.search.hybrid import (
    SEMANTIC,
    DenseLegs,
    GrepLegs,
    HybridRetriever,
    SearchHit,
    Weights,
)
from sensei_engine.search.plan import Leg, Origin, SearchPlan, SubQuery, plan
from sensei_engine.search.rerank import Candidate, Reranker, RerankMode, RerankUsageSink
from sensei_engine.search.route import Lane, QueryClass, QueryRouter, Route, classify

log = get_logger(__name__)

SNIPPET_LINES = 12
SNIPPET_LINE_CHARS = 400  # a snippet's line longer than this is cut (minified code)
SNIPPET_LEAD = 2  # lines shown before the first line with a query term
RERANK_CANDIDATES = 50
MAX_TERMS = 24
MAX_EXACT = 8

WHY: Mapping[Leg, str] = {
    Leg.LEX_CODE: "keyword",
    Leg.LEX_PATH: "keyword",
    Leg.LEX_SUMMARY: "keyword",
    Leg.GREP: "keyword",
    Leg.SYM: "symbol",
    Leg.SEM_CODE: "semantic",
    Leg.SEM_SUMMARY: "semantic",
    Leg.PPR: "graph",
}
REWRITES = (Origin.STRUCTURAL, Origin.BEHAVIOURAL)
_WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{2,}")
# Words of a question that highlight nothing useful.
_STOP_WORDS = (
    "the and for are was were with what when where which who why how does did done this that "
    "these those from into onto over under than then there their them they its it's has have "
    "had not but can could should would will shall may might must any all each every some "
    "about after before between through during out off our your you get gets got make makes "
    "made use used uses using find show tell list give code file files function functions "
    "class method happens happen work works way ways"
)
_STOP = frozenset(_STOP_WORDS.split())


class SemanticNotReadyError(Exception):
    """A semantic search of a snapshot that has no embeddings yet."""


class SearchMode(enum.StrEnum):
    AUTO = "auto"  # every leg the plan has that can run
    KEYWORD = "keyword"  # BM25, names and exact text: no embeddings
    SEMANTIC = "semantic"  # embeddings only


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    mode: SearchMode = SearchMode.AUTO
    languages: tuple[str, ...] = ()
    path: str | None = None  # a path prefix

    def key(self) -> str:
        """The same for the same search (what a cached answer and a page cursor are tied to)."""
        parts = [self.text, self.mode.value, sorted(set(self.languages)), self.path]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Highlight:
    line: int  # 1-based, of the file
    start: int  # UTF-16 code units
    end: int  # exclusive


@dataclass(frozen=True, slots=True)
class Snippet:
    start_line: int
    lines: tuple[str, ...]
    highlights: tuple[Highlight, ...]


@dataclass(frozen=True, slots=True)
class Result:
    id: str  # the symbol key, or "<path>#L<start>-<end>": stable within the commit
    chunk_id: uuid.UUID  # the snippet's chunk
    symbol_key: str | None
    name: str
    kind: str  # the definition's kind, or "file" (a region of a file outside any definition)
    path: str
    language: str | None
    lane: Lane
    start_line: int
    end_line: int
    chunk_start: int
    chunk_end: int
    score: float  # 0-1, comparable within the answer
    why: tuple[str, ...]  # strongest first, never empty


@dataclass(frozen=True, slots=True)
class Interpretation:
    query_class: str  # symbol, literal, regex, conceptual, flow, architecture, impact
    rewrites: tuple[str, ...]  # the model's rewrites that ran besides the query
    literals: tuple[str, ...]
    path_filter: str | None


@dataclass(frozen=True, slots=True)
class ExactQuery:
    kind: str  # literal, regex, symbol, path
    query: str


@dataclass(frozen=True, slots=True)
class Answer:
    query: str
    interpretation: Interpretation
    lanes: Mapping[Lane, tuple[Result, ...]]  # every lane, best first
    exact_queries: tuple[ExactQuery, ...]
    terms: tuple[str, ...]  # what snippets highlight
    semantic_pending: bool
    summaries_pending: bool
    reranked: bool


class SearchService:
    """Runs searches (see the module). ``router`` and ``reranker`` are optional: without them
    the rules route, and the fused order stands."""

    def __init__(
        self,
        *,
        weights: Weights | None = None,
        router: QueryRouter | None = None,
        reranker: Reranker | None = None,
        rerank_candidates: int = RERANK_CANDIDATES,
    ) -> None:
        self.weights = weights or Weights()
        self._router = router
        self._reranker = reranker
        self._rerank_candidates = rerank_candidates

    @property
    def reranker(self) -> Reranker | None:
        """The reranker its answers go through (the agent's final rerank reuses it)."""
        return self._reranker

    async def route(
        self, query: str, *, scope: str, on_llm_usage: LLMUsageSink | None = None
    ) -> Route:
        if self._router is None:
            return classify(query).route
        return await self._router.route(query, scope=scope, on_usage=on_llm_usage)

    async def search(
        self,
        db: AsyncSession,
        query: SearchQuery,
        *,
        repo_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        scope: str,
        grep: GrepLegs | None = None,
        dense: DenseLegs | None = None,
        graph: GraphSnapshot | None = None,
        on_llm_usage: LLMUsageSink | None = None,
        on_rerank_usage: RerankUsageSink | None = None,
    ) -> Answer:
        """Every result of ``query`` on the snapshot, by lane. ``scope`` is the org asking (the
        router caches per org); ``grep`` runs the exact-text legs, ``dense`` the semantic ones
        (None: the snapshot has no embeddings yet), ``graph`` (the snapshot's) the graph leg,
        except in semantic mode."""
        route = await self.route(query.text, scope=scope, on_llm_usage=on_llm_usage)
        found = narrow(plan(route), query.mode, semantic_ready=dense is not None)
        around = graph if query.mode is not SearchMode.SEMANTIC else None
        retriever = HybridRetriever(weights=self.weights, grep=grep, dense=dense, graph=around)
        filters = Filters(languages=query.languages, path_prefix=query.path)
        rankings, _ = await retriever.rankings(
            db, found, repo_id=repo_id, snapshot_id=snapshot_id, filters=filters
        )
        hits = await retriever.results(
            db, found, rankings, repo_id=repo_id, snapshot_id=snapshot_id, filters=filters
        )
        ordered, scores, reranked = await self._rerank(
            db, query.text, hits, repo_id=repo_id, on_usage=on_rerank_usage, graph=graph
        )
        lanes: dict[Lane, list[Result]] = {lane: [] for lane in Lane}
        seen: set[str] = set()
        for hit, score in zip(ordered, scores, strict=True):
            result = _result(hit, score, self.weights)
            if result.id not in seen:
                seen.add(result.id)
                lanes[result.lane].append(result)
        return Answer(
            query=query.text,
            interpretation=interpret(route, found, query.path),
            lanes={lane: tuple(results) for lane, results in lanes.items()},
            exact_queries=exact_queries(route),
            terms=terms_of(route),
            semantic_pending=dense is None,
            summaries_pending=not await has_summaries(db, repo_id, snapshot_id),
            reranked=reranked,
        )

    async def _rerank(
        self,
        db: AsyncSession,
        query: str,
        hits: Sequence[SearchHit],
        *,
        repo_id: uuid.UUID,
        on_usage: RerankUsageSink | None,
        graph: GraphSnapshot | None = None,
    ) -> tuple[list[SearchHit], list[float], bool]:
        top = hits[: self._rerank_candidates]
        if self._reranker is None or len(top) < 2:
            return await rerank(None, query, hits, {}, candidates=0)
        contents = await read_contents(db, repo_id, [hit.chunk_id for hit in top])
        return await rerank(
            self._reranker,
            query,
            hits,
            contents,
            candidates=self._rerank_candidates,
            on_usage=on_usage,
            graph=graph,
        )


async def rerank(
    reranker: Reranker | None,
    query: str,
    hits: Sequence[SearchHit],
    contents: Mapping[uuid.UUID, ChunkText],
    *,
    candidates: int,
    on_usage: RerankUsageSink | None = None,
    graph: GraphSnapshot | None = None,
    mode: RerankMode = RerankMode.INTERACTIVE,
    on_llm_usage: LLMUsageSink | None = None,
) -> tuple[list[SearchHit], list[float], bool]:
    """The hits in their final order, with their 0-1 scores, and whether the cross-encoder
    ordered them: its picks of the best ``candidates`` first (their relevance), then the rest
    in fused order (their fused score, scaled below the picks). With the snapshot's ``graph``,
    each definition's callers and callees go along (the listwise model reads them, 3.6.5).
    ``mode`` is the search box's (the cross-encoder only) unless asked (the evals: ``agent``)."""
    fused = [hit.score for hit in hits]
    top = list(hits[:candidates])
    if reranker is None or len(top) < 2:
        return list(hits), _scaled(fused, 1.0), False
    shown = [
        Candidate(
            id=str(n),
            code=_definition_text(hit, contents.get(hit.chunk_id)),
            path=hit.path,
            layer=hit.lane.value,
            graph_neighbourhood=(
                neighbourhood(graph, hit.symbol_key)
                if graph is not None and hit.symbol_key is not None
                else None
            ),
        )
        for n, hit in enumerate(top)
    ]
    ranking = await reranker.rerank(
        query, shown, mode, on_rerank_usage=on_usage, on_llm_usage=on_llm_usage
    )
    if not ranking.scored:
        return list(hits), _scaled(fused, 1.0), False
    picked = [int(item.candidate.id) for item in ranking.items]
    relevance = [item.score or 0.0 for item in ranking.items]
    chosen = set(picked)
    rest = [n for n in range(len(top)) if n not in chosen] + list(range(len(top), len(hits)))
    floor = min(relevance, default=1.0)
    ordered = [hits[n] for n in picked] + [hits[n] for n in rest]
    scores = [round(r, 6) for r in relevance] + _scaled([fused[n] for n in rest], floor * 0.99)
    return ordered, scores, True


def narrow(found: SearchPlan, mode: SearchMode, *, semantic_ready: bool) -> SearchPlan:
    """The plan's legs for ``mode`` (see the module)."""
    if mode is SearchMode.KEYWORD:
        return replace(found, legs=tuple(sub for sub in found.legs if sub.leg not in SEMANTIC))
    if mode is SearchMode.SEMANTIC:
        if not semantic_ready:
            msg = "the snapshot has no embeddings yet"
            raise SemanticNotReadyError(msg)
        legs = tuple(sub for sub in found.legs if sub.leg in SEMANTIC)
        if not legs:  # a symbol or pattern query's plan: its words, embedded
            literals = found.route.literals
            legs = (SubQuery(Leg.SEM_CODE, found.route.query, Origin.ORIGINAL, literals),)
        return replace(found, legs=legs)
    return found


def _scaled(scores: Sequence[float], top: float) -> list[float]:
    """``scores`` (best first) scaled so the best is ``top``."""
    best = max(scores, default=0.0)
    if best <= 0:
        return [0.0 for _ in scores]
    return [round(top * score / best, 6) for score in scores]


def _why(hit: SearchHit, weights: Weights) -> tuple[str, ...]:
    strength: dict[str, float] = {}
    for leg_name, rank in hit.legs.items():
        leg = Leg(leg_name)
        share = weights.legs.get(leg, 0.0) / (weights.rrf_k + rank)
        reason = WHY[leg]
        strength[reason] = strength.get(reason, 0.0) + share
    return tuple(sorted(strength, key=lambda reason: -strength[reason])) or ("keyword",)


def _result(hit: SearchHit, score: float, weights: Weights) -> Result:
    region = hit.symbol_key is None
    return Result(
        id=hit.symbol_key or f"{hit.path}#L{hit.start_line}-{hit.end_line}",
        chunk_id=hit.chunk_id,
        symbol_key=hit.symbol_key,
        name=hit.name or basename(hit.path),
        kind="file" if region else hit.kind,
        path=hit.path,
        language=hit.language,
        lane=hit.lane,
        start_line=hit.start_line,
        end_line=hit.end_line,
        chunk_start=hit.chunk_start,
        chunk_end=hit.chunk_end,
        score=score,
        why=_why(hit, weights),
    )


def interpret(route: Route, found: SearchPlan, path: str | None) -> Interpretation:
    if route.query_class is QueryClass.REGEX:
        query_class = "regex" if route.regex is not None else "literal"
    else:
        query_class = route.query_class.value
    rewrites = [sub.text for sub in found.legs if sub.origin in REWRITES]
    return Interpretation(
        query_class=query_class,
        rewrites=tuple(dict.fromkeys(rewrites)),
        literals=route.literals,
        path_filter=path,
    )


def exact_queries(route: Route) -> tuple[ExactQuery, ...]:
    """What "pivot to exact" runs: the pattern, or the symbol asked for, then the literals,
    the other names, the paths."""
    found: list[ExactQuery] = []
    if route.regex is not None:
        found.append(ExactQuery("regex", route.regex))
    if route.symbol:
        found.append(ExactQuery("symbol", route.symbol))
    found += [ExactQuery("literal", lit) for lit in route.literals if lit not in route.paths]
    found += [ExactQuery("symbol", name) for name in route.identifiers]
    found += [ExactQuery("path", path) for path in route.paths]
    return tuple(dict.fromkeys(found))[:MAX_EXACT]


def terms_of(route: Route) -> tuple[str, ...]:
    """What a snippet highlights: the literals, the names, and the query's other words."""
    words = [w for w in _WORD.findall(route.query) if w.lower() not in _STOP]
    names = [route.symbol] if route.symbol else []
    found = [*route.literals, *names, *route.identifiers, *words]
    unique = dict.fromkeys(term for term in found if term.strip())
    return tuple(unique)[:MAX_TERMS]


async def has_summaries(db: AsyncSession, repo_id: uuid.UUID, snapshot_id: uuid.UUID) -> bool:
    """Whether the snapshot's live chunks include summaries (2.8) to search."""
    found = await db.scalar(
        text(
            "SELECT EXISTS (SELECT FROM public.chunks c WHERE c.repo_id = :repo_id "
            "AND c.snapshot_id = :snapshot_id AND c.view = 'summary' "
            "AND c.generation = (SELECT s.chunk_generation FROM public.snapshots s "
            "WHERE s.id = :snapshot_id AND s.repo_id = :repo_id))"
        ),
        {"repo_id": repo_id, "snapshot_id": snapshot_id},
    )
    return bool(found)


@dataclass(frozen=True, slots=True)
class ChunkText:
    start_line: int
    lines: tuple[str, ...]


async def read_contents(
    db: AsyncSession, repo_id: uuid.UUID, chunk_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, ChunkText]:
    """The chunks' lines (each chunk's content is its lines of the file, exactly)."""
    wanted = sorted(set(chunk_ids))
    if not wanted:
        return {}
    rows = await db.execute(
        select(CodeChunk.id, CodeChunk.start_line, CodeChunk.content).where(
            CodeChunk.repo_id == repo_id, CodeChunk.id.in_(wanted)
        )
    )
    return {row[0]: ChunkText(row[1], tuple(text_lines(row[2]))) for row in rows}


def _definition_text(hit: SearchHit, chunk: ChunkText | None) -> str:
    """The hit's lines within its chunk: the definition found in a chunk of several, or the
    chunk."""
    if chunk is None:
        return ""
    first = max(hit.start_line, chunk.start_line) - chunk.start_line
    last = min(hit.end_line, chunk.start_line + len(chunk.lines) - 1) - chunk.start_line
    return "\n".join(chunk.lines[first : last + 1]) if first <= last else "\n".join(chunk.lines)


def _utf16(text: str, index: int) -> int:
    return len(text[:index].encode("utf-16-le")) // 2


def _spans(line: str, pattern: re.Pattern[str] | None) -> list[tuple[int, int]]:
    """Where the terms are in ``line`` (characters), overlaps merged."""
    if pattern is None:
        return []
    merged: list[tuple[int, int]] = []
    for match in pattern.finditer(line):
        start, end = match.span()
        if start == end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _pattern(terms: Sequence[str]) -> re.Pattern[str] | None:
    ordered = sorted({t for t in terms if t.strip()}, key=len, reverse=True)  # longest first
    if not ordered:
        return None
    return re.compile("|".join(re.escape(term) for term in ordered), re.IGNORECASE)


def snippet(result: Result, chunk: ChunkText | None, terms: Sequence[str]) -> Snippet:
    """At most :data:`SNIPPET_LINES` lines of the result, from just before its first line with
    a term (else from its start), with the terms highlighted."""
    if chunk is None or not chunk.lines:
        return Snippet(result.start_line, (), ())
    chunk_end = chunk.start_line + len(chunk.lines) - 1
    first = max(result.start_line, chunk.start_line)
    last = min(result.end_line, chunk_end)
    if first > last:  # the result's lines aren't the chunk's: show the chunk
        first, last = chunk.start_line, chunk_end
    pattern = _pattern(terms)
    lines = {
        n: chunk.lines[n - chunk.start_line][:SNIPPET_LINE_CHARS] for n in range(first, last + 1)
    }
    hit_line = next((n for n, line in lines.items() if _spans(line, pattern)), first)
    start = max(first, min(hit_line - SNIPPET_LEAD, last - SNIPPET_LINES + 1))
    shown = range(start, min(last, start + SNIPPET_LINES - 1) + 1)
    highlights = tuple(
        Highlight(n, _utf16(lines[n], a), _utf16(lines[n], b))
        for n in shown
        for a, b in _spans(lines[n], pattern)
    )
    return Snippet(start, tuple(lines[n] for n in shown), highlights)


async def snippets(
    db: AsyncSession, repo_id: uuid.UUID, results: Sequence[Result], terms: Sequence[str]
) -> dict[str, Snippet]:
    """Each result's snippet, by result id."""
    chunks = await read_contents(db, repo_id, [result.chunk_id for result in results])
    return {result.id: snippet(result, chunks.get(result.chunk_id), terms) for result in results}
