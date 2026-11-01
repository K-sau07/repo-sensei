"""The dependency graph by level (6.2): ``GET …/deps`` and ``GET …/deps/edge-evidence``.

``level`` is ``service``, ``package``, ``module`` (the default), ``file`` or ``symbol`` (see
:mod:`sensei_engine.deps`); ``focus`` narrows it to a node and its neighbours ``depth`` hops out;
``types`` (comma-separated) and ``min_conf`` filter the code's edges before they are counted.
Each node comes with where it is drawn (the level's layout, computed once per graph and kept
beside the snapshot as an artifact), its layer, size and cluster (its group a level up); each
edge with its counts by type, its surest edge's confidence and a few evidence lines. Asked with
``Accept: application/x-msgpack``, the answer is MessagePack, gzipped (a level of 20,000 nodes
travels small). ``edge-evidence`` is the exact import and call lines behind one edge.
"""

from __future__ import annotations

import asyncio
import gzip
from collections import OrderedDict
from collections.abc import Callable
from typing import Annotated, Any, cast

import msgpack
import orjson
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from sensei_api.deps import DbSession, RedisClient
from sensei_api.graphs import GraphStore
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import COMMIT_HEADER, RefParam
from sensei_api.routers.graph import limit_graph
from sensei_core.errors import NotFoundError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import RepoFile, Snapshot
from sensei_core.storage import Bucket, ObjectStorage, artifact_key
from sensei_engine.deps import (
    MANIFESTS,
    MAX_FORCE,
    DepGraph,
    Grouping,
    Level,
    aggregate,
    evidence,
    focus,
    grouping,
    layout,
)
from sensei_engine.graph.snapshot import GraphSnapshot

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["deps"])

MAX_NODES = 20_000  # sent at once (more: the largest, and ``truncated``)
MSGPACK = "application/x-msgpack"
CACHED = 16  # levels kept in memory (per process)
MAX_LAYOUT_BYTES = 64 << 20
_pack = cast("Callable[[object], bytes]", msgpack.packb)  # pyright: ignore[reportUnknownMemberType]
_unpack = cast("Callable[..., object]", msgpack.unpackb)  # pyright: ignore[reportUnknownMemberType]


class DepNodeOut(BaseModel):
    id: str
    label: str
    layer: str
    size: int
    cluster: str | None
    x: float
    y: float


class SampleOut(BaseModel):
    path: str
    line: int


class DepEdgeOut(BaseModel):
    source: str
    target: str
    weight: int
    types: dict[str, int]
    confidence: float
    samples: list[SampleOut]


class ClusterOut(BaseModel):
    id: str
    nodes: int


class DepsOut(BaseModel):
    level: Level
    sha: str
    layout: str  # "force", or "spiral" for a level too big for one
    total: int  # the level's nodes, before any cut
    truncated: bool
    nodes: list[DepNodeOut]
    edges: list[DepEdgeOut]
    clusters: list[ClusterOut]


class EvidenceOut(BaseModel):
    source: str  # symbol keys
    target: str
    type: str
    confidence: float
    provenance: str
    path: str | None
    line: int | None


class EdgeEvidenceOut(BaseModel):
    total: int
    edges: list[EvidenceOut]


Key = tuple[Any, ...]


class DepsCache:
    """Levels (and their layouts) in memory, the most recently used kept."""

    def __init__(self, size: int = CACHED) -> None:
        self._held: OrderedDict[Key, Any] = OrderedDict()
        self._size = size

    def get(self, key: Key) -> Any:
        found = self._held.get(key)
        if found is not None:
            self._held.move_to_end(key)
        return found

    def put(self, key: Key, value: Any) -> None:
        self._held[key] = value
        self._held.move_to_end(key)
        while len(self._held) > self._size:
            self._held.popitem(last=False)


def _cache(request: Request) -> DepsCache:
    state = request.app.state
    found: DepsCache | None = getattr(state, "deps_cache", None)
    if found is None:
        found = DepsCache()
        state.deps_cache = found
    return found


async def _roots(request: Request, db: DbSession, snapshot: Snapshot) -> Grouping:
    key = ("roots", snapshot.id)
    cache = _cache(request)
    held: Grouping | None = cache.get(key)
    if held is not None:
        return held
    names = "|".join(name.replace(".", r"\.") for name in sorted(MANIFESTS))
    paths = await db.scalars(
        select(RepoFile.path).where(
            RepoFile.snapshot_id == snapshot.id,
            RepoFile.path.regexp_match(rf"(^|/)({names}|[^/]+\.csproj)$"),
        )
    )
    listed = cast("list[object]", (snapshot.summary or {}).get("frameworks") or [])
    frameworks = [cast("dict[str, Any]", f) for f in listed if isinstance(f, dict)]
    found = grouping(list(paths), frameworks)
    cache.put(key, found)
    return found


async def _graph(
    request: Request, db: DbSession, repo: RepoDep, ref: str | None
) -> tuple[Snapshot, GraphSnapshot]:
    snapshot = await resolve_snapshot(db, repo, ref)
    store: GraphStore = request.app.state.graph_store
    storage: ObjectStorage = request.app.state.storage
    return snapshot, await store.get(db, storage, snapshot)


def _types(raw: str | None) -> frozenset[str] | None:
    if not raw:
        return None
    return frozenset(t.strip() for t in raw.split(",") if t.strip()) or None


async def _positions(
    request: Request, snapshot: Snapshot, graph: GraphSnapshot, whole: DepGraph
) -> dict[str, tuple[float, float]]:
    """The level's layout: in memory, else the artifact kept for this graph, else worked out
    (off the event loop) and kept."""
    key = ("layout", snapshot.id, graph.version, whole.level)
    cache = _cache(request)
    held: dict[str, tuple[float, float]] | None = cache.get(key)
    if held is not None:
        return held
    storage: ObjectStorage | None = getattr(request.app.state, "storage", None)
    name = artifact_key(
        snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, f"layout-{whole.level}.msgpack"
    )
    placed: dict[str, tuple[float, float]] | None = None
    if storage is not None:
        data = await storage.get_bytes(Bucket.ARTIFACTS, name, max_bytes=MAX_LAYOUT_BYTES)
        if data:
            stored = cast("dict[str, Any]", _unpack(gzip.decompress(data)))
            if stored.get("version") == graph.version:
                at = cast("dict[str, list[float]]", stored["at"])
                placed = {k: (float(v[0]), float(v[1])) for k, v in at.items()}
    if placed is None:
        placed = await asyncio.to_thread(layout, whole)
        if storage is not None:
            blob = _pack({"version": graph.version, "at": placed})
            await storage.put_bytes(Bucket.ARTIFACTS, name, gzip.compress(blob))
    cache.put(key, placed)
    return placed


@router.get("/deps", response_model=DepsOut, responses={200: {"content": {MSGPACK: {}}}})
async def deps(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    level: Level = "module",
    focus_on: Annotated[str | None, Query(alias="focus", min_length=1, max_length=2000)] = None,
    depth: Annotated[int, Query(ge=1, le=3)] = 1,
    types: Annotated[str | None, Query(max_length=500)] = None,
    min_conf: Annotated[float, Query(ge=0, le=1)] = 0.0,
    ref: RefParam = None,
) -> Any:
    """The level's nodes (where each is drawn) and edges (counted by type), or a focus of it."""
    await limit_graph(request, redis, access)
    snapshot, graph = await _graph(request, db, repo, ref)
    roots = await _roots(request, db, snapshot)
    cache = _cache(request)
    plain_key = ("level", snapshot.id, graph.version, level)
    whole: DepGraph | None = cache.get(plain_key)
    if whole is None:
        whole = await asyncio.to_thread(aggregate, graph, level, roots)
        cache.put(plain_key, whole)
    shown = whole
    wanted = _types(types)
    if wanted is not None or min_conf > 0:
        shown = await asyncio.to_thread(
            aggregate, graph, level, roots, types=wanted, min_conf=min_conf
        )
    if focus_on is not None:
        if focus_on not in shown.nodes:
            raise NotFoundError(f"No {level} named {focus_on!r}.", code="deps_focus_not_found")
        shown = focus(shown, focus_on, depth)
    placed = await _positions(request, snapshot, graph, whole)
    out = _out(snapshot, whole, shown, placed, focus_on)
    if MSGPACK in request.headers.get("accept", ""):
        body = gzip.compress(_pack(orjson.loads(out.model_dump_json())))
        return Response(
            body,
            media_type=MSGPACK,
            headers={"Content-Encoding": "gzip", COMMIT_HEADER: snapshot.commit_sha},
        )
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    return out


def _out(
    snapshot: Snapshot,
    whole: DepGraph,
    shown: DepGraph,
    placed: dict[str, tuple[float, float]],
    keep: str | None,
) -> DepsOut:
    ids = sorted(shown.nodes)
    truncated = len(ids) > MAX_NODES
    if truncated:
        sizes = {key: shown.nodes[key].size for key in ids}
        biggest = sorted(ids, key=lambda key: (-sizes[key], key))[:MAX_NODES]
        chosen: set[str] = set(biggest)
        if keep:
            chosen.add(keep)
        ids = sorted(chosen)
    kept = set(ids)
    clusters: dict[str, int] = {}
    nodes: list[DepNodeOut] = []
    for key in ids:
        node = shown.nodes[key]
        x, y = placed.get(key, (0.0, 0.0))
        nodes.append(
            DepNodeOut(
                id=key, label=node.label, layer=node.layer, size=node.size,
                cluster=node.cluster, x=x, y=y,
            )
        )  # fmt: skip
        if node.cluster is not None:
            clusters[node.cluster] = clusters.get(node.cluster, 0) + 1
    edges = [
        DepEdgeOut(
            source=src, target=dst, weight=edge.weight, types=dict(edge.types),
            confidence=edge.confidence,
            samples=[SampleOut(path=p, line=n) for p, n in edge.samples],
        )
        for (src, dst), edge in shown.edges.items()
        if src in kept and dst in kept
    ]  # fmt: skip
    return DepsOut(
        level=shown.level,
        sha=snapshot.commit_sha,
        layout="force" if len(whole.nodes) <= MAX_FORCE else "spiral",
        total=len(shown.nodes),
        truncated=truncated,
        nodes=nodes,
        edges=edges,
        clusters=[ClusterOut(id=k, nodes=v) for k, v in sorted(clusters.items())],
    )


@router.get("/deps/edge-evidence", response_model=EdgeEvidenceOut)
async def edge_evidence(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    src: Annotated[str, Query(min_length=1, max_length=2000)],
    dst: Annotated[str, Query(min_length=1, max_length=2000)],
    level: Level = "module",
    types: Annotated[str | None, Query(max_length=500)] = None,
    min_conf: Annotated[float, Query(ge=0, le=1)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ref: RefParam = None,
) -> EdgeEvidenceOut:
    """The code's edges behind the level's edge from ``src`` to ``dst``: each import or call,
    with its line, surest first."""
    await limit_graph(request, redis, access)
    snapshot, graph = await _graph(request, db, repo, ref)
    roots = await _roots(request, db, snapshot)
    found, total = await asyncio.to_thread(
        evidence, graph, level, roots, src, dst,
        types=_types(types), min_conf=min_conf, limit=limit,
    )  # fmt: skip
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    return EdgeEvidenceOut(
        total=total,
        edges=[
            EvidenceOut(
                source=e.src,
                target=e.dst,
                type=e.type,
                confidence=e.confidence,
                provenance=e.provenance,
                path=e.path,
                line=e.line,
            )
            for e in found
        ],
    )
