"""Impact (6.4): ``GET …/impact?symbol=`` or ``?files=`` (repeated): what a change to a symbol
(its key, or a name: each definition of it), or to some files, may break, sorted into routes,
screens, tests, tables and other symbols, each ranked by its score (the surest way back to the
change, decayed by distance; see :mod:`sensei_engine.impact`), and the scores by node
(``heat``) for drawing. ``min_conf`` (0.5 by default) is the least sure edge followed; edges a
model inferred never are. ``ref`` picks the snapshot as in browsing.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import COMMIT_HEADER, RefParam
from sensei_api.routers.graph import limit_graph
from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.storage import ObjectStorage
from sensei_engine.graph.snapshot import GraphSnapshot, Node
from sensei_engine.graph.store import GraphStore
from sensei_engine.graph.trace import TraceError, resolve_entry
from sensei_engine.impact import CATEGORIES, MIN_CONF, Affected, changed_nodes, impact

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["impact"])

MAX_FILES = 200


class ImpactNodeOut(BaseModel):
    key: str
    name: str
    kind: str
    path: str | None
    start_line: int
    end_line: int


class AffectedOut(ImpactNodeOut):
    score: float
    distance: int
    via: str | None  # the next node on its surest way to the change
    edge: str | None  # the edge it takes there


class AffectedByCategory(BaseModel):
    routes: list[AffectedOut]
    screens: list[AffectedOut]
    tests: list[AffectedOut]
    tables: list[AffectedOut]
    symbols: list[AffectedOut]


class ImpactOut(BaseModel):
    sha: str
    changed: list[ImpactNodeOut]
    affected: AffectedByCategory
    heat: dict[str, float]
    truncated: bool


def _node(node: Node) -> ImpactNodeOut:
    return ImpactNodeOut(
        key=node.symbol_key, name=node.qualified_name or node.name, kind=node.kind,
        path=node.path, start_line=node.start_line, end_line=node.end_line,
    )  # fmt: skip


def _affected(item: Affected) -> AffectedOut:
    return AffectedOut(
        **_node(item.node).model_dump(),
        score=round(item.score, 4),
        distance=item.distance,
        via=item.via,
        edge=item.edge.value if item.edge is not None else None,
    )


def _changed(graph: GraphSnapshot, symbol: str | None, files: list[str]) -> list[Node]:
    symbols: list[str] = []
    if symbol is not None:
        if graph.node(symbol) is not None:
            symbols = [symbol]
        else:
            try:
                symbols = [n.symbol_key for n in resolve_entry(graph, symbol, "symbol")]
            except TraceError:
                symbols = []
    found = changed_nodes(graph, symbols=symbols, files=files)
    if not found:
        raise NotFoundError(
            "Nothing in the code graph matches the change.", code="impact_not_found"
        )
    return found


@router.get("/impact", response_model=ImpactOut)
async def impact_of(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    symbol: Annotated[str | None, Query(min_length=1, max_length=2000)] = None,
    files: Annotated[list[str] | None, Query(max_length=MAX_FILES)] = None,
    min_conf: Annotated[float, Query(ge=0, le=1)] = MIN_CONF,
    ref: RefParam = None,
) -> ImpactOut:
    """What the change may break, by category, ranked."""
    if (symbol is None) == (not files):
        raise InvalidError("Give a symbol or files, not both.", code="impact_change")
    await limit_graph(request, redis, access)
    snapshot = await resolve_snapshot(db, repo, ref)
    store: GraphStore = request.app.state.graph_store
    storage: ObjectStorage = request.app.state.storage
    graph = await store.get(db, storage, snapshot)
    changed = _changed(graph, symbol, files or [])
    result = await asyncio.to_thread(impact, graph, changed, min_conf=min_conf)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    by = {name: [_affected(a) for a in result.affected.get(name, [])] for name in CATEGORIES}
    return ImpactOut(
        sha=snapshot.commit_sha,
        changed=[_node(n) for n in result.changed],
        affected=AffectedByCategory(**by),
        heat=result.heat,
        truncated=result.truncated,
    )
