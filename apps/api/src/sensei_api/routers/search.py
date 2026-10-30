"""Search (2.13): one endpoint the search page and the agent's ``search`` tool share.

``POST /orgs/{org}/repos/{repo}/search`` runs :class:`~sensei_engine.search.service.SearchService`
on the snapshot ``ref`` picks (the commit in ``X-Commit-Sha``) and answers its results by lane,
a page of each, with snippets (at most 12 lines, the query's terms highlighted in UTF-16
columns).

- **Pages.** An answer is kept in Redis for ``SENSEI_SEARCH_CACHE_SECONDS``, so every page of a
  lane comes from one ranking; ``next`` holds each lane's cursor (opaque: the search it belongs
  to, the lane, the offset). "More" of a lane is the same body with ``layers: [lane]`` and its
  cursor; the other lanes come back empty, the counts as they were.
- **Partial.** Without embeddings the keyword, symbol and exact legs answer, with
  ``partial.semantic_pending``; ``mode: "semantic"`` answers ``409 semantic_not_ready``.
- **Limits.** ``SENSEI_SEARCH_RATE_PER_MINUTE`` searches per user a minute (``429`` with
  ``Retry-After``). What the query router's model and the reranker cost is recorded in the usage
  ledger, by user.
- **Errors.** ``404 repo_not_indexed`` (never indexed), ``409 not_indexed`` (the snapshot has
  no live chunks yet), ``422`` ``validation_failed`` / ``invalid_query`` / ``invalid_cursor``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import uuid
from collections.abc import Container
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.deps import DbSession, RedisClient
from sensei_api.graphs import GraphStore
from sensei_api.orgs import OrgAccessDep
from sensei_api.ratelimit import TokenBucket
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import (
    COMMIT_HEADER,
    SearchUnavailableError,
    _grep,  # pyright: ignore[reportPrivateUsage] - the exact search's own runner
    searchable,
)
from sensei_core.config import Settings
from sensei_core.errors import ConflictError, DomainError, InvalidError, RateLimitedError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.logging import get_logger
from sensei_core.models import Repo, Snapshot
from sensei_core.orgs.service import OrgAccess
from sensei_core.storage import ObjectStorage
from sensei_core.usage import CostCalculator
from sensei_engine.embed.provider import EmbeddingProvider
from sensei_engine.embed.provider import usage_ledger as embedding_ledger
from sensei_engine.embed.voyage import VoyageClient
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.llm.anthropic_client import AnthropicClient
from sensei_engine.llm.ledger import usage_ledger as llm_ledger
from sensei_engine.search.bm25 import Filters
from sensei_engine.search.dense import EmbeddedLegs
from sensei_engine.search.grep import (
    MAX_LIMIT,
    GrepBusyError,
    GrepQuery,
    GrepUnavailableError,
    InvalidQueryError,
)
from sensei_engine.search.hybrid import DenseLegs, Weights, grep_places, path_globs
from sensei_engine.search.plan import SubQuery
from sensei_engine.search.rerank import LLMReranker, Reranker, VoyageReranker
from sensei_engine.search.rerank import usage_ledger as rerank_ledger
from sensei_engine.search.route import Lane, QueryRouter
from sensei_engine.search.service import (
    SNIPPET_LINES,
    Answer,
    Result,
    SearchMode,
    SearchQuery,
    SearchService,
    SemanticNotReadyError,
    snippets,
)
from sensei_engine.snapshots import UnsafePathError, check_path
from sensei_engine.symbols.extract import SymbolKind

log = get_logger(__name__)

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["search"])

MAX_QUERY = 500
DEFAULT_LIMIT = 8
MAX_LIMIT_PER_LANE = 50
MAX_OFFSET = 10_000
CACHE_PREFIX = "search:v1:"
REF_TYPE = "search"
EMBEDDED_PREFIX = "search:embedded:"
EMBEDDED_SECONDS = 24 * 3600
_ANSWER = TypeAdapter(Answer)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=MAX_QUERY)
    layers: list[Lane] | None = Field(default=None, max_length=len(Lane))
    langs: list[Annotated[str, Field(min_length=1, max_length=40)]] | None = Field(
        default=None, max_length=20
    )
    path: str | None = Field(default=None, max_length=1024, description="A path prefix.")
    ref: str | None = Field(default=None, max_length=255)
    mode: SearchMode = SearchMode.AUTO
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT_PER_LANE, description="Per lane.")
    cursor: str | None = Field(default=None, max_length=512)


class SearchHighlight(BaseModel):
    line: int
    start: int
    end: int


class SearchSnippet(BaseModel):
    start_line: int
    lines: list[str] = Field(max_length=SNIPPET_LINES)
    highlights: list[SearchHighlight]


Why = Literal["keyword", "semantic", "symbol", "graph"]


class SearchResultHit(BaseModel):
    id: str
    symbol_key: str | None
    name: str
    kind: SymbolKind | Literal["file"]
    path: str
    language: str | None
    layer: Lane
    start_line: int
    end_line: int
    snippet: SearchSnippet
    score: float
    why: list[Why]


class SearchInterpretation(BaseModel):
    query_class: Literal[
        "symbol", "literal", "regex", "conceptual", "flow", "architecture", "impact"
    ] = Field(serialization_alias="class")
    rewrites: list[str]
    literals: list[str]
    path_filter: str | None


class SearchLanes(BaseModel):
    ui: list[SearchResultHit]
    api: list[SearchResultHit]
    svc: list[SearchResultHit]
    db: list[SearchResultHit]
    other: list[SearchResultHit]


class SearchLaneCounts(BaseModel):
    ui: int
    api: int
    svc: int
    db: int
    other: int


class SearchLaneCursors(BaseModel):
    ui: str | None
    api: str | None
    svc: str | None
    db: str | None
    other: str | None


class SearchExactQuery(BaseModel):
    kind: Literal["literal", "regex", "symbol", "path"]
    query: str


class SearchPartial(BaseModel):
    semantic_pending: bool
    summaries_pending: bool


class SearchResponse(BaseModel):
    query: str
    interpretation: SearchInterpretation
    lanes: SearchLanes
    counts: SearchLaneCounts
    next: SearchLaneCursors
    exact_queries: list[SearchExactQuery]
    partial: SearchPartial


def get_search_service(request: Request) -> SearchService:
    """The process's search service (tests put their own on ``app.state``)."""
    state = request.app.state
    service: SearchService | None = getattr(state, "search_service", None)
    if service is None:
        settings: Settings = state.settings
        anthropic = AnthropicClient(settings.anthropic)
        query_router = QueryRouter(
            anthropic, state.redis, timeout_seconds=settings.search.router_timeout_seconds
        )
        reranker = (
            Reranker(VoyageReranker(settings.voyage, state.http), LLMReranker(anthropic))
            if settings.search.rerank
            else None
        )
        service = SearchService(
            weights=Weights.from_settings(settings.search), router=query_router, reranker=reranker
        )
        state.search_service = service
    return service


def cost_calculator(request: Request) -> CostCalculator:
    state = request.app.state
    calc: CostCalculator | None = getattr(state, "cost_calculator", None)
    if calc is None:
        settings: Settings = state.settings
        calc = CostCalculator.from_settings(settings.pricing)
        state.cost_calculator = calc
    return calc


def embedding_provider(request: Request) -> EmbeddingProvider | None:
    """The deployment's embedding provider (the one the chunks were embedded with); None
    without a Voyage key. Tests set their own on ``app.state.embeddings``."""
    state = request.app.state
    found: EmbeddingProvider | None = getattr(state, "embeddings", None)
    if found is None:
        settings: Settings = state.settings
        if not settings.voyage.configured:
            return None
        found = VoyageClient(settings.voyage, state.http)
        state.embeddings = found
    return found


async def embeddings_ready(db: AsyncSession, redis: Redis, snapshot: Snapshot) -> bool:
    """Whether every chunk of the snapshot's live generation has its vector. Once it has, the
    answer holds for that generation: it is kept in Redis (a day) instead of read again."""
    marker = f"{EMBEDDED_PREFIX}{snapshot.id}:{snapshot.chunk_generation}"
    try:
        if await redis.get(marker):
            return True
    except RedisError:
        log.warning("embeddings marker unreadable")
    missing = await db.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM public.chunks WHERE repo_id = :repo_id "
            "AND snapshot_id = :snapshot_id AND generation = :generation "
            "AND embedding IS NULL)"
        ),
        {
            "repo_id": snapshot.repo_id,
            "snapshot_id": snapshot.id,
            "generation": snapshot.chunk_generation,
        },
    )
    if missing:
        return False
    try:
        await redis.set(marker, "1", ex=EMBEDDED_SECONDS)
    except RedisError:
        log.warning("embeddings marker not kept")
    return True


async def semantic_legs(
    request: Request,
    db: AsyncSession,
    redis: Redis,
    snapshot: Snapshot,
    costs: CostCalculator,
    *,
    org_id: uuid.UUID,
) -> EmbeddedLegs | None:
    """The semantic legs, once the snapshot's embeddings are ready (else None: partial mode);
    the query's embedding is billed to the org."""
    provider = embedding_provider(request)
    if provider is None or not await embeddings_ready(db, redis, snapshot):
        return None
    return EmbeddedLegs(
        provider, on_usage=embedding_ledger(db, costs, org_id=org_id, ref_type=REF_TYPE)
    )


async def search_graph(
    request: Request, db: AsyncSession, snapshot: Snapshot
) -> GraphSnapshot | None:
    """The snapshot's graph, for the graph leg; None until its graph stage has run, or when it
    can't be read (the search goes on without the leg)."""
    store: GraphStore | None = getattr(request.app.state, "graph_store", None)
    if store is None:
        return None
    storage: ObjectStorage = request.app.state.storage
    try:
        return await store.get(db, storage, snapshot)
    except ConflictError:
        return None
    except Exception:  # the graph is a signal, not what a search stands on
        log.exception("graph unavailable for search", snapshot_id=str(snapshot.id))
        return None


class SnapshotGrep:
    """The plan's exact-text legs, on the unpacked snapshot (as ``/grep`` searches it), within
    the request's path prefix. A leg that can't run (search busy or unavailable) is left out of
    the answer and kept in ``failed``: an answer that had only such legs isn't "no matches".
    The legs run at once; the files they may report are read once, on the request's session."""

    def __init__(self, request: Request, db: AsyncSession, snapshot: Snapshot) -> None:
        self._request = request
        self._db = db
        self._snapshot = snapshot
        self._files: Container[str] | None = None
        self._reading = asyncio.Lock()  # one statement at a time on the session
        self.failed: DomainError | None = None

    async def _searchable(self) -> Container[str]:
        async with self._reading:
            if self._files is None:
                self._files = await searchable(self._request, self._db, self._snapshot)
            return self._files

    async def __call__(self, sub: SubQuery, limit: int, filters: Filters) -> list[tuple[str, int]]:
        try:
            query = GrepQuery(
                sub.text,
                regex=sub.regex,
                globs=path_globs(filters.path_prefix),
                limit=min(limit * 4, MAX_LIMIT),
            )
            result = await _grep(self._request, self._snapshot, query, await self._searchable())
        except InvalidQueryError:
            return []
        except GrepBusyError:
            log.warning("search left out its exact legs", error="GrepBusyError")
            self.failed = SearchUnavailableError(
                "Too many searches are running; try again in a moment.", code="search_busy"
            )
            return []
        except (GrepUnavailableError, SearchUnavailableError) as exc:
            log.warning("search left out its exact legs", error=type(exc).__name__)
            self.failed = SearchUnavailableError("Exact search isn't available right now.")
            return []
        return grep_places(result.hits, limit)


def _path_prefix(path: str | None) -> str | None:
    if path is None or not path.strip("/ "):
        return None
    try:
        relative = check_path(path.strip().strip("/"))
    except UnsafePathError:
        raise InvalidError("That isn't a path inside the repository.") from None
    return relative + "/" if path.rstrip().endswith("/") else relative


def _cursor(key: str, lane: Lane, offset: int) -> str:
    raw = json.dumps({"k": key[:24], "l": lane.value, "o": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _read_cursor(cursor: str, key: str) -> tuple[Lane, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        found: Any = json.loads(raw)
        lane, offset = Lane(found["l"]), int(found["o"])
        if found["k"] != key[:24] or not 0 <= offset <= MAX_OFFSET:
            raise ValueError
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise InvalidError(
            "That page is of another search; search again.", code="invalid_cursor"
        ) from None
    return lane, offset


async def _cached(redis: Redis, key: str) -> Answer | None:
    try:
        raw: bytes | str | None = await redis.get(key)
    except RedisError as exc:
        log.warning("search cache unavailable", error=type(exc).__name__)
        return None
    if raw is None:
        return None
    try:
        return _ANSWER.validate_json(raw)
    except ValidationError:
        return None


async def _keep(redis: Redis, key: str, answer: Answer, seconds: int) -> None:
    if seconds <= 0:
        return
    try:
        await redis.set(key, _ANSWER.dump_json(answer), ex=seconds)
    except RedisError as exc:
        log.warning("search cache unavailable", error=type(exc).__name__)


async def _answer(
    request: Request,
    db: AsyncSession,
    query: SearchQuery,
    *,
    redis: Redis,
    access: OrgAccess,
    repo: Repo,
    snapshot: Snapshot,
    dense: DenseLegs | None,
    graph: GraphSnapshot | None,
    costs: CostCalculator,
    cache_key: str,
    cache_seconds: int,
) -> Answer:
    """The kept answer to ``query``, or a new one (kept unless some of its legs couldn't run)."""
    answer = await _cached(redis, cache_key)
    if answer is not None:
        return answer
    org_id, user_id = access.org.id, access.user.id
    grep = SnapshotGrep(request, db, snapshot)
    try:
        answer = await get_search_service(request).search(
            db,
            query,
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            scope=str(org_id),
            grep=grep,
            dense=dense,
            graph=graph,
            on_llm_usage=llm_ledger(db, costs, org_id=org_id, user_id=user_id, ref_type=REF_TYPE),
            on_rerank_usage=rerank_ledger(
                db, costs, org_id=org_id, user_id=user_id, ref_type=REF_TYPE
            ),
        )
    except SemanticNotReadyError:  # pragma: no cover - checked by the caller
        raise ConflictError("Semantic search isn't ready.", code="semantic_not_ready") from None
    if grep.failed is not None:
        if not any(answer.lanes.values()):  # its exact legs were all it had
            raise grep.failed
    else:  # an answer missing legs isn't kept: the next search runs them
        await _keep(redis, cache_key, answer, cache_seconds)
    return answer


def _hit(result: Result, snippet: Any) -> SearchResultHit:
    return SearchResultHit(
        id=result.id,
        symbol_key=result.symbol_key,
        name=result.name,
        kind=result.kind,  # pyright: ignore[reportArgumentType] - checked by the model
        path=result.path,
        language=result.language,
        layer=result.lane,
        start_line=result.start_line,
        end_line=result.end_line,
        snippet=SearchSnippet(
            start_line=snippet.start_line,
            lines=list(snippet.lines),
            highlights=[
                SearchHighlight(line=h.line, start=h.start, end=h.end) for h in snippet.highlights
            ],
        ),
        score=result.score,
        why=list(result.why),  # pyright: ignore[reportArgumentType]
    )


@router.post(
    "/search",
    response_model=SearchResponse,
    responses={
        200: {
            "headers": {
                COMMIT_HEADER: {
                    "description": "The commit the response was read from.",
                    "schema": {"type": "string"},
                }
            }
        },
        409: {"description": "Not searchable yet (``not_indexed``, ``semantic_not_ready``)"},
        422: {"description": "``validation_failed``, ``invalid_query`` or ``invalid_cursor``"},
        429: {"description": "Too many searches (``rate_limited``, with ``Retry-After``)"},
    },
)
async def search(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    body: SearchRequest,
) -> SearchResponse:
    """The snapshot's results for ``q``, by lane (see the module)."""
    text = body.q.strip()
    if not text:
        raise InvalidError("Type something to search for.", code="invalid_query")
    query = SearchQuery(
        text=text,
        mode=body.mode,
        languages=tuple(dict.fromkeys(body.langs or ())),
        path=_path_prefix(body.path),
    )
    key = query.key()
    wanted = list(dict.fromkeys(body.layers)) if body.layers else list(Lane)
    offsets = dict.fromkeys(wanted, 0)
    if body.cursor is not None:
        lane, offset = _read_cursor(body.cursor, key)
        if lane not in wanted:
            raise InvalidError("That page is of another lane.", code="invalid_cursor")
        offsets = {lane: offset}

    settings: Settings = request.app.state.settings
    bucket = TokenBucket(redis, name="search", per_minute=settings.search.rate_per_minute)
    wait = await bucket.take(str(access.user.id))
    if wait is not None:
        raise RateLimitedError(
            "Too many searches; try again in a moment.", headers={"Retry-After": str(wait)}
        )

    snapshot = await resolve_snapshot(db, repo, body.ref)
    if snapshot.chunk_count is None or snapshot.chunk_generation is None:
        raise ConflictError(
            "Search appears once this commit's indexing has chunked its files.",
            code="not_indexed",
        )
    costs = cost_calculator(request)
    dense = await semantic_legs(request, db, redis, snapshot, costs, org_id=access.org.id)
    if query.mode is SearchMode.SEMANTIC and dense is None:
        raise ConflictError(
            "Semantic search appears once this commit's embeddings are ready.",
            code="semantic_not_ready",
        )
    graph = await search_graph(request, db, snapshot)
    cache_key = (
        f"{CACHE_PREFIX}{snapshot.id}:{snapshot.chunk_generation}:{dense is not None}:"
        f"{graph.version if graph is not None else '-'}:{key}"
    )
    answer = await _answer(
        request,
        db,
        query,
        redis=redis,
        access=access,
        repo=repo,
        snapshot=snapshot,
        dense=dense,
        graph=graph,
        costs=costs,
        cache_key=cache_key,
        cache_seconds=settings.search.cache_seconds,
    )

    pages: dict[Lane, list[Result]] = {lane: [] for lane in Lane}
    after: dict[Lane, str | None] = dict.fromkeys(Lane)
    for lane, offset in offsets.items():
        results = answer.lanes.get(lane, ())
        pages[lane] = list(results[offset : offset + body.limit])
        if offset + body.limit < len(results):
            after[lane] = _cursor(key, lane, offset + body.limit)
    shown = [result for page in pages.values() for result in page]
    cut = await snippets(db, repo.id, shown, answer.terms)

    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    lanes = {lane.value: [_hit(r, cut[r.id]) for r in pages[lane]] for lane in Lane}
    return SearchResponse(
        query=answer.query,
        interpretation=SearchInterpretation(
            query_class=answer.interpretation.query_class,  # pyright: ignore[reportArgumentType]
            rewrites=list(answer.interpretation.rewrites),
            literals=list(answer.interpretation.literals),
            path_filter=answer.interpretation.path_filter,
        ),
        lanes=SearchLanes(**lanes),
        counts=SearchLaneCounts(**{lane.value: len(answer.lanes.get(lane, ())) for lane in Lane}),
        next=SearchLaneCursors(**{lane.value: after[lane] for lane in Lane}),
        exact_queries=[
            SearchExactQuery(kind=q.kind, query=q.query)  # pyright: ignore[reportArgumentType]
            for q in answer.exact_queries
        ],
        partial=SearchPartial(
            semantic_pending=answer.semantic_pending, summaries_pending=answer.summaries_pending
        ),
    )
