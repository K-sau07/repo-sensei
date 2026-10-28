"""The code graph of an indexed snapshot (3.6): a symbol's neighbours (callers, callees, what it
imports, ...) and the surest path between two symbols.

``symbol`` (and ``from``/``to``) is a node's key: a definition's symbol key
(``py app/crud.py/create_user().``) or a graph node's (``external_symbol py:sqlmodel/Session.add``).
``types`` is a comma-separated list of edge types (none: every type); ``min_conf`` drops edges
less sure than it. ``ref`` picks the snapshot as in browsing; the commit read is in
``X-Commit-Sha``. A snapshot whose graph isn't built yet answers ``409 graph_not_ready``; an
unknown key ``404 symbol_not_found``. ``SENSEI_API_GRAPH_RATE_PER_MINUTE`` requests per user a
minute (``429 rate_limited``, with ``Retry-After``); traversals run off the event loop.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from sensei_api.deps import DbSession, RedisClient
from sensei_api.graphs import GraphStore
from sensei_api.orgs import OrgAccessDep
from sensei_api.ratelimit import TokenBucket
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import COMMIT_HEADER, RefParam
from sensei_core.config import Settings
from sensei_core.errors import InvalidError, NotFoundError, RateLimitedError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import EdgeType, Provenance
from sensei_core.storage import ObjectStorage
from sensei_engine.graph.confidence import path_confidence
from sensei_engine.graph.snapshot import MAX_DEPTH, MAX_LIMIT, GraphSnapshot, Hop, Node
from sensei_engine.search.route import Lane

router = APIRouter(prefix="/orgs/{org}/repos/{repo}/graph", tags=["graph"])

_COMMIT_HEADER_DOC = {
    COMMIT_HEADER: {
        "description": "The commit the response was read from.",
        "schema": {"type": "string"},
    }
}
_ERRORS = {
    404: {"description": "No such symbol in the graph (``symbol_not_found``)"},
    409: {"description": "The graph isn't built yet (``graph_not_ready``)"},
    422: {"description": "``validation_failed`` or ``invalid_types``"},
    429: {"description": "Too many graph requests (``rate_limited``, with ``Retry-After``)"},
}
KeyParam = Annotated[str, Query(min_length=1, max_length=2000)]
TypesParam = Annotated[
    str | None, Query(max_length=400, description="Edge types, comma-separated (none: all)")
]
MinConfParam = Annotated[float, Query(ge=0, le=1)]


class GraphNodeOut(BaseModel):
    symbol_key: str
    kind: str
    name: str
    qualified_name: str
    path: str | None
    start_line: int
    end_line: int
    lang: str
    lane: Lane  # the swimlane it is drawn in, as search hits' (externals: other)


class GraphEvidence(BaseModel):
    path: str
    line: int | None


class GraphEdgeOut(BaseModel):
    type: EdgeType
    confidence: float
    provenance: Provenance
    evidence: GraphEvidence | None
    direction: Literal["out", "in"]  # out: from ``via`` to the hop's node


class GraphHopOut(BaseModel):
    node: GraphNodeOut
    edge: GraphEdgeOut
    depth: int
    via: str  # the key of the node it was reached from


class GraphNeighborsOut(BaseModel):
    root: GraphNodeOut
    hops: list[GraphHopOut]
    truncated: bool  # ``limit`` cut the list short


class GraphPathOut(BaseModel):
    found: bool
    hops: list[GraphHopOut]
    confidence: float  # the product of the path's edges' (0 when none was found)


def _node(node: Node) -> GraphNodeOut:
    return GraphNodeOut(
        symbol_key=node.symbol_key,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        path=node.path,
        start_line=node.start_line,
        end_line=node.end_line,
        lang=node.lang,
        lane=Lane(node.lane),
    )


def _hop(hop: Hop) -> GraphHopOut:
    edge = hop.edge
    evidence = (
        GraphEvidence(path=edge.evidence_path, line=edge.evidence_line)
        if edge.evidence_path is not None
        else None
    )
    return GraphHopOut(
        node=_node(hop.node),
        edge=GraphEdgeOut(
            type=edge.type,
            confidence=round(edge.confidence, 4),
            provenance=edge.provenance,
            evidence=evidence,
            direction=hop.direction,
        ),
        depth=hop.depth,
        via=hop.via,
    )


def _types(raw: str | None) -> frozenset[EdgeType] | None:
    if raw is None or not raw.strip():
        return None
    names = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [name for name in names if name not in EdgeType.__members__.values()]
    if unknown:
        known = ", ".join(t.value for t in EdgeType)
        raise InvalidError(
            f"Unknown edge types: {', '.join(unknown)} (known: {known}).", code="invalid_types"
        )
    return frozenset(EdgeType(name) for name in names)


async def _limit(request: Request, redis: RedisClient, access: OrgAccessDep) -> None:
    settings: Settings = request.app.state.settings
    bucket = TokenBucket(redis, name="graph", per_minute=settings.api.graph_rate_per_minute)
    wait = await bucket.take(str(access.user.id))
    if wait is not None:
        raise RateLimitedError(
            "Too many graph requests; try again in a moment.", headers={"Retry-After": str(wait)}
        )


async def _graph(
    request: Request, response: Response, repo: RepoDep, db: DbSession, ref: str | None
) -> GraphSnapshot:
    snapshot = await resolve_snapshot(db, repo, ref)
    store: GraphStore = request.app.state.graph_store
    storage: ObjectStorage = request.app.state.storage
    graph = await store.get(db, storage, snapshot)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    return graph


def _known(graph: GraphSnapshot, key: str) -> Node:
    node = graph.node(key)
    if node is None:
        raise NotFoundError("No such symbol in this commit's graph.", code="symbol_not_found")
    return node


@router.get(
    "/neighbors",
    response_model=GraphNeighborsOut,
    responses={200: {"headers": _COMMIT_HEADER_DOC}, **_ERRORS},
)
async def neighbors(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    symbol: KeyParam,
    direction: Literal["out", "in", "both"] = "out",
    types: TypesParam = None,
    depth: Annotated[int, Query(ge=1, le=MAX_DEPTH)] = 1,
    min_conf: MinConfParam = 0.0,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    ref: RefParam = None,
) -> GraphNeighborsOut:
    """What ``symbol`` reaches (``out``: its callees, imports, ...), what reaches it (``in``:
    its callers, ...) or both, nearest first, each node once."""
    wanted = _types(types)
    await _limit(request, redis, access)
    graph = await _graph(request, response, repo, db, ref)
    root = _known(graph, symbol)
    hops, truncated = await asyncio.to_thread(
        graph.neighbors,
        symbol,
        direction=direction,
        edge_types=wanted,
        depth=depth,
        min_conf=min_conf,
        limit=limit,
    )
    return GraphNeighborsOut(root=_node(root), hops=[_hop(h) for h in hops], truncated=truncated)


@router.get(
    "/path",
    response_model=GraphPathOut,
    responses={200: {"headers": _COMMIT_HEADER_DOC}, **_ERRORS},
)
async def path(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    source: Annotated[str, Query(alias="from", min_length=1, max_length=2000)],
    target: Annotated[str, Query(alias="to", min_length=1, max_length=2000)],
    types: TypesParam = None,
    min_conf: MinConfParam = 0.0,
    ref: RefParam = None,
) -> GraphPathOut:
    """The surest path from ``from`` to ``to`` along the edges (the highest product of
    confidences)."""
    wanted = _types(types)
    await _limit(request, redis, access)
    graph = await _graph(request, response, repo, db, ref)
    _known(graph, source)
    _known(graph, target)
    hops = await asyncio.to_thread(
        graph.shortest_path, source, target, edge_types=wanted, min_conf=min_conf
    )
    if not hops:
        return GraphPathOut(found=False, hops=[], confidence=0.0)
    confidence = path_confidence(h.edge.confidence for h in hops)
    return GraphPathOut(found=True, hops=[_hop(h) for h in hops], confidence=round(confidence, 4))
