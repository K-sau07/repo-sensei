"""Tracing a feature across the layers (3.12): from where it starts (a component, a route, a
symbol) to the tables it reads and writes, or to ``to``; the top paths, each hop with the edge
that proves it.

``POST …/trace`` takes ``{from, kind?, to?, min_conf?, direction?}`` (``both`` by default: from
where the entry is reached, a page, to where it goes; ``entry_index`` says which hop it is); ``GET
…/trace?from=`` is the same, for deep links (the code viewer's "Trace this"). ``from`` (and ``to``)
is a node's key, a route (``POST /api/v1/items/``), a file, or a name (see
:func:`sensei_engine.graph.trace.resolve_entry`). ``ref`` picks the snapshot as in browsing; the
commit read is in ``X-Commit-Sha``. A snapshot whose graph isn't built yet answers ``409
graph_not_ready``; ``linked`` says whether its cross-layer links are in it yet (the links stage:
without them a trace stays in the code). An entry the graph doesn't have is ``404
trace_entry_not_found``. A trace counts against the user's graph requests per minute (``429
rate_limited``), and runs off the event loop.

A break carries what the graph knows of why: a client call no endpoint matches has the reason
3.10 found (``detail``: the path exists under another method, the URL is dynamic, ...).

A path that went down through a client call comes back up (3.22): its hops from
``return_index`` on are the **return leg**, from the code that made the call back up the path,
each followed by what it does with the response, an outcome node whose ``outcome`` says what (a
key stored, a page navigated to, state set, an error shown; ``on: error`` in an error's branch).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from sensei_api.deps import DbSession, RedisClient
from sensei_api.graphs import GraphStore, links_done
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import COMMIT_HEADER, RefParam
from sensei_api.routers.graph import limit_graph
from sensei_core.errors import NotFoundError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import CodeNode, EdgeType, NodeKind, Provenance, Snapshot
from sensei_core.storage import ObjectStorage
from sensei_engine.graph.snapshot import GraphSnapshot, Node
from sensei_engine.graph.trace import (
    BreakKind,
    Direction,
    EntryKind,
    Layer,
    TraceError,
    TraceHop,
    TracePath,
    table_model,
    trace_feature,
)
from sensei_engine.search.route import Lane

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["trace"])

_COMMIT_HEADER_DOC = {
    COMMIT_HEADER: {
        "description": "The commit the response was read from.",
        "schema": {"type": "string"},
    }
}
_ERRORS = {
    404: {"description": "Nothing in the graph is the entry (``trace_entry_not_found``)"},
    409: {"description": "The graph isn't built yet (``graph_not_ready``)"},
    429: {"description": "Too many graph requests (``rate_limited``, with ``Retry-After``)"},
}
EntryText = Annotated[str, Field(min_length=1, max_length=2000)]


class TraceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: EntryText = Field(alias="from")
    kind: EntryKind = "auto"  # what ``from`` is (auto: a key, a route, a file, then a name)
    to: EntryText | None = None
    # both: where the entry is reached from (back to a page) and where it goes (on to the data)
    direction: Direction = "both"
    min_conf: Annotated[float, Field(ge=0, le=1)] = 0.0


class TraceModelOut(BaseModel):
    """The class a table is mapped from."""

    symbol_key: str
    path: str
    lines: tuple[int, int]


class TraceOutcomeOut(BaseModel):
    """What the client does with a response (a return leg's outcome)."""

    kind: Literal["storage", "navigate", "state", "error"]
    action: str  # as written: localStorage.setItem, navigate, router.push, setOrders, ...
    target: str | None  # the key, the path, the setter (None: not known)
    value: str | None  # what it is given, short
    after_call: str | None = None  # the client call whose result flows into it (its key)
    on: Literal["error"] | None = None  # in an error's branch (a catch, an onError)


class TraceNodeOut(BaseModel):
    kind: str
    name: str
    qualified_name: str
    symbol_key: str
    path: str | None
    lines: tuple[int, int] | None  # None: no lines (a node in no file)
    lane: Lane
    model: TraceModelOut | None = None  # a table's ORM class
    outcome: TraceOutcomeOut | None = None  # a return leg's outcome: what it is


class TraceEvidence(BaseModel):
    path: str
    line: int | None


class TraceEdgeOut(BaseModel):
    type: EdgeType
    confidence: float
    provenance: Provenance
    evidence: TraceEvidence | None
    alternatives: int  # the targets its call site has (2+: ambiguous)


class TraceHopOut(BaseModel):
    layer: Layer
    node: TraceNodeOut
    edge: TraceEdgeOut | None  # None: the entry


class TraceBreakOut(BaseModel):
    after_hop: int
    reason: str
    # gap: the graph is missing the next hop; end: server code that touches no data (a DTO
    # built); limit: the trace's hop limit, the code goes on
    kind: BreakKind = "gap"
    detail: str | None = None  # what the graph knows of why


class TracePathOut(BaseModel):
    hops: list[TraceHopOut]
    path_confidence: float  # the product of its edges'
    layers: list[Layer]
    reaches_goal: bool  # the data layer, or ``to``
    breaks: list[TraceBreakOut]
    entry_index: int  # the entry's hop: the hops before it are where it is reached from
    reaches_origin: bool  # those start at a page (or the app's entry module)
    # The return leg's first hop (None: none): back up to what the client does with the response.
    return_index: int | None = None


class TraceOut(BaseModel):
    entries: list[TraceNodeOut]  # where the paths start (several: the entry was ambiguous)
    goal: TraceNodeOut | None  # ``to``'s node
    linked: bool  # the cross-layer links are in the graph (the links stage finished)
    paths: list[TracePathOut]


def _node(
    graph: GraphSnapshot, node: Node, outcomes: dict[str, TraceOutcomeOut] | None = None
) -> TraceNodeOut:
    model = table_model(graph, node) if node.kind == "table" else None
    return TraceNodeOut(
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        symbol_key=node.symbol_key,
        path=node.path,
        lines=(node.start_line, node.end_line) if node.start_line > 0 else None,
        lane=Lane(node.lane),
        model=TraceModelOut(
            symbol_key=model.symbol_key,
            path=model.path,
            lines=(model.start_line, model.end_line),
        )
        if model is not None and model.path is not None
        else None,
        outcome=(outcomes or {}).get(node.symbol_key),
    )


def _hop(
    graph: GraphSnapshot, hop: TraceHop, outcomes: dict[str, TraceOutcomeOut] | None = None
) -> TraceHopOut:
    edge = hop.edge
    return TraceHopOut(
        layer=hop.layer,
        node=_node(graph, hop.node, outcomes),
        edge=TraceEdgeOut(
            type=hop.edge_type or edge.type,
            confidence=round(edge.confidence, 4),
            provenance=edge.provenance,
            evidence=TraceEvidence(path=edge.evidence_path, line=edge.evidence_line)
            if edge.evidence_path is not None
            else None,
            alternatives=hop.alternatives,
        )
        if edge is not None
        else None,
    )


def _path(
    graph: GraphSnapshot,
    path: TracePath,
    details: dict[str, str],
    outcomes: dict[str, TraceOutcomeOut] | None = None,
) -> TracePathOut:
    return TracePathOut(
        hops=[_hop(graph, hop, outcomes) for hop in path.hops],
        path_confidence=round(path.confidence, 4),
        layers=path.layers,
        reaches_goal=path.reaches_goal,
        entry_index=path.entry_index,
        reaches_origin=path.reaches_origin,
        return_index=path.return_index,
        breaks=[
            TraceBreakOut(
                after_hop=b.after_hop, reason=b.reason, kind=b.kind, detail=details.get(b.node)
            )
            for b in path.breaks
        ],
    )


async def _break_details(db: DbSession, snapshot: Snapshot, keys: set[str]) -> dict[str, str]:
    """Why each break's node stops (``attrs.unmatched_reason``: 3.10's for a client call)."""
    if not keys:
        return {}
    rows = await db.execute(
        select(CodeNode.symbol_key, CodeNode.attrs).where(
            CodeNode.snapshot_id == snapshot.id, CodeNode.symbol_key.in_(sorted(keys))
        )
    )
    found: dict[str, str] = {}
    for key, attrs in rows.tuples():
        reason: Any = (attrs or {}).get("unmatched_reason")
        if isinstance(reason, str) and reason:
            found[key] = reason
    return found


def outcome_of(attrs: dict[str, Any] | None) -> TraceOutcomeOut | None:
    """An outcome node's attrs (:mod:`sensei_engine.link.outcomes`) as the API says them."""
    found = attrs or {}
    kind, action = found.get("kind"), found.get("action")
    if kind not in ("storage", "navigate", "state", "error") or not isinstance(action, str):
        return None

    def text(key: str) -> str | None:
        value = found.get(key)
        return value if isinstance(value, str) and value else None

    return TraceOutcomeOut(
        kind=kind,
        action=action,
        target=text("target"),
        value=text("value"),
        after_call=text("after_call"),
        on="error" if found.get("on") == "error" else None,
    )


async def _outcomes(
    db: DbSession, snapshot: Snapshot, keys: set[str]
) -> dict[str, TraceOutcomeOut]:
    """What each return leg's outcome is, from its node's attrs."""
    if not keys:
        return {}
    rows = await db.execute(
        select(CodeNode.symbol_key, CodeNode.attrs).where(
            CodeNode.snapshot_id == snapshot.id, CodeNode.symbol_key.in_(sorted(keys))
        )
    )
    return {key: found for key, attrs in rows.tuples() if (found := outcome_of(attrs)) is not None}


async def _trace(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    body: TraceRequest,
    ref: str | None,
) -> TraceOut:
    await limit_graph(request, redis, access)
    snapshot = await resolve_snapshot(db, repo, ref)
    store: GraphStore = request.app.state.graph_store
    storage: ObjectStorage = request.app.state.storage
    graph = await store.get(db, storage, snapshot)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    try:
        result = await asyncio.to_thread(
            trace_feature,
            graph,
            body.source,
            kind=body.kind,
            to=body.to,
            min_conf=body.min_conf,
            direction=body.direction,
        )
    except TraceError as exc:
        raise NotFoundError(str(exc), code="trace_entry_not_found") from None
    details = await _break_details(
        db, snapshot, {b.node for path in result.paths for b in path.breaks}
    )
    outcomes = await _outcomes(
        db,
        snapshot,
        {
            hop.node.symbol_key
            for path in result.paths
            for hop in path.hops
            if hop.node.kind == NodeKind.CLIENT_OUTCOME
        },
    )
    return TraceOut(
        entries=[_node(graph, node) for node in result.entries],
        goal=_node(graph, result.goal) if result.goal is not None else None,
        linked=await links_done(db, snapshot),
        paths=[_path(graph, path, details, outcomes) for path in result.paths],
    )


@router.post(
    "/trace", response_model=TraceOut, responses={200: {"headers": _COMMIT_HEADER_DOC}, **_ERRORS}
)
async def trace(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    body: TraceRequest,
    ref: RefParam = None,
) -> TraceOut:
    """The surest paths from ``from`` across the layers to the data (or to ``to``), at most
    three, best first."""
    return await _trace(
        request=request,
        response=response,
        access=access,
        repo=repo,
        db=db,
        redis=redis,
        body=body,
        ref=ref,
    )


@router.get(
    "/trace", response_model=TraceOut, responses={200: {"headers": _COMMIT_HEADER_DOC}, **_ERRORS}
)
async def trace_link(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    source: Annotated[str, Query(alias="from", min_length=1, max_length=2000)],
    kind: EntryKind = "auto",
    to: Annotated[str | None, Query(min_length=1, max_length=2000)] = None,
    min_conf: Annotated[float, Query(ge=0, le=1)] = 0.0,
    direction: Direction = "both",
    ref: RefParam = None,
) -> TraceOut:
    """``POST …/trace`` for a deep link (``?from=``)."""
    body = TraceRequest.model_validate(
        {"from": source, "kind": kind, "to": to, "min_conf": min_conf, "direction": direction}
    )
    return await _trace(
        request=request,
        response=response,
        access=access,
        repo=repo,
        db=db,
        redis=redis,
        body=body,
        ref=ref,
    )
