"""The dependency graph by level (6.2): ``GET …/deps`` and ``GET …/deps/edge-evidence``.

``level`` is ``service``, ``package``, ``module`` (the default), ``file`` or ``symbol`` (see
:mod:`sensei_engine.deps`); ``focus`` narrows it to a node and its neighbours ``depth`` hops out;
``types`` (comma-separated) and ``min_conf`` filter the code's edges before they are counted
(``min_conf`` is 0.5 unless asked: calls matched by their names alone are guesses, left out).
Each node comes with where it is drawn (the level's layout, computed once per graph and kept
beside the snapshot as an artifact), its layer, size and cluster (its group a level up); each
edge with its counts by type, its surest edge's confidence and a few evidence lines. Asked with
``Accept: application/x-msgpack``, the answer is MessagePack, gzipped (a level of 20,000 nodes
travels small). ``edge-evidence`` is the exact import and call lines behind one edge.

``deps/dsm`` (6.3) is the level as a dependency structure matrix (rows and columns by layer,
then name; a cell, the edges from its row to its column, flagged when both are in one cycle),
with its cycles (Tarjan's strongly connected components, each with the few edges that close it)
and each group's coupling (afferent, efferent, instability); ``deps/cycles`` is the cycles
alone.

``deps/packages`` (6.5) is the third-party packages: each declared one (its versions, where it
is declared, whether only for development) with how many files import it and which, and what
the code imports that is declared nowhere. A snapshot indexed before packages were inventoried
says so (``inventoried`` false): its declared list is empty until it is indexed again.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
from collections import OrderedDict
from collections.abc import Callable
from typing import Annotated, Any, cast

import msgpack
import orjson
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import COMMIT_HEADER, RefParam
from sensei_api.routers.graph import limit_graph
from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.logging import get_logger
from sensei_core.models import RepoFile, Snapshot
from sensei_core.storage import Bucket, ObjectStorage, artifact_key
from sensei_engine.deps import (
    MANIFESTS,
    MAX_FORCE,
    MIN_CONF,
    Cycle,
    DepGraph,
    Grouping,
    Level,
    aggregate,
    coupling,
    cycles,
    dsm,
    evidence,
    focus,
    grouping,
    layout,
)
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.graph.store import GraphStore
from sensei_engine.third_party import USED_IN, PackageInUse, packages_in_use, usage

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["deps"])

MAX_NODES = 20_000  # sent at once (more: the largest, and ``truncated``)
MSGPACK = "application/x-msgpack"
CACHED = 48  # levels (whole and as filtered), their cycles and layouts kept (per process)
log = get_logger(__name__)

MAX_LAYOUT_BYTES = 64 << 20
LAYOUT_FORMAT = 2  # bump when the layout code changes
MAX_DSM = 2_500  # groups a matrix is drawn for (its cells are its edges; drawn 3 px each)
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

    def holds(self, value: Any) -> bool:
        """Whether ``value`` itself is kept (by identity)."""
        return any(held is value for held in self._held.values())

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


async def _level(
    request: Request,
    db: DbSession,
    repo: RepoDep,
    *,
    level: Level,
    types: str | None,
    min_conf: float,
    ref: str | None,
) -> tuple[Snapshot, GraphSnapshot, DepGraph, DepGraph]:
    """The snapshot, its graph, the whole level, and the level as filtered."""
    snapshot, graph = await _graph(request, db, repo, ref)
    roots = await _roots(request, db, snapshot)
    cache = _cache(request)
    plain_key = ("level", snapshot.id, graph.version, level)
    whole: DepGraph | None = cache.get(plain_key)
    if whole is None:
        whole = await asyncio.to_thread(aggregate, graph, level, roots)
        cache.put(plain_key, whole)
    wanted = _types(types)
    if wanted is None and min_conf <= 0:
        return snapshot, graph, whole, whole
    # Kept only as the web app asks for it (the default bar, every type): other filters are
    # anyone's to vary, and a level can be big, so they are worked out each time.
    kept = wanted is None and min_conf == MIN_CONF
    shown_key = (*plain_key, MIN_CONF)
    shown: DepGraph | None = cache.get(shown_key) if kept else None
    if shown is None:
        shown = await asyncio.to_thread(
            aggregate, graph, level, roots, types=wanted, min_conf=min_conf
        )
        if kept:
            cache.put(shown_key, shown)
    return snapshot, graph, whole, shown


async def _cycles(request: Request, snapshot: Snapshot, shown: DepGraph) -> list[Cycle]:
    """The level's cycles, each with what closes it (seconds for a tangle of thousands): kept
    beside the level they are of, while it is kept."""
    cache = _cache(request)
    key = ("cycles", snapshot.id, id(shown))
    held: tuple[DepGraph, list[Cycle]] | None = cache.get(key)
    if held is not None and held[0] is shown:  # the very level (not one at a reused id)
        return held[1]
    found = await asyncio.to_thread(cycles, shown)
    if cache.holds(shown):  # a level not kept (an unusual filter) doesn't keep its cycles
        cache.put(key, (shown, found))
    return found


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
    # The artifact is this layout only for the same graph, layout code and groups: the groups'
    # ids come from the grouping, which can change while the graph doesn't.
    stamp = {"version": graph.version, "format": LAYOUT_FORMAT, "ids": _ids_digest(whole)}
    placed: dict[str, tuple[float, float]] | None = None
    if storage is not None:
        data = await storage.get_bytes(Bucket.ARTIFACTS, name, max_bytes=MAX_LAYOUT_BYTES)
        placed = _stored_layout(data, stamp) if data else None
    if placed is None:
        placed = await asyncio.to_thread(layout, whole)
        if storage is not None:
            blob = _pack({**stamp, "at": placed})
            await storage.put_bytes(Bucket.ARTIFACTS, name, gzip.compress(blob))
    cache.put(key, placed)
    return placed


def _ids_digest(whole: DepGraph) -> str:
    return hashlib.sha256("\0".join(sorted(whole.nodes)).encode()).hexdigest()[:32]


def _stored_layout(data: bytes, stamp: dict[str, Any]) -> dict[str, tuple[float, float]] | None:
    """The kept layout, if it is this one (see :func:`_positions`); an unreadable one is
    worked out again."""
    try:
        stored = cast("dict[str, Any]", _unpack(gzip.decompress(data)))
        if any(stored.get(k) != v for k, v in stamp.items()):
            return None
        at = cast("dict[str, list[float]]", stored["at"])
        return {k: (float(v[0]), float(v[1])) for k, v in at.items()}
    except Exception:  # a corrupt artifact (any decoding error): laid out again
        log.warning("deps.layout_unreadable", exc_info=True)
        return None


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
    min_conf: Annotated[float, Query(ge=0, le=1)] = MIN_CONF,
    ref: RefParam = None,
) -> Any:
    """The level's nodes (where each is drawn) and edges (counted by type), or a focus of it."""
    await limit_graph(request, redis, access)
    snapshot, graph, whole, shown = await _level(
        request, db, repo, level=level, types=types, min_conf=min_conf, ref=ref
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
    min_conf: Annotated[float, Query(ge=0, le=1)] = MIN_CONF,
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


class DsmNodeOut(BaseModel):
    id: str
    label: str
    layer: str
    size: int
    afferent: int  # Ca: the groups that depend on it
    efferent: int  # Ce: the groups it depends on
    instability: float | None  # Ce / (Ca + Ce)


class DsmCellOut(BaseModel):
    row: int  # the depending group's index in ``order``
    col: int  # the depended-on group's
    count: int
    cycle: bool  # both in one cycle


class CycleEdgeOut(BaseModel):
    source: str
    target: str
    weight: int


class CycleOut(BaseModel):
    nodes: list[str]
    edges: list[CycleEdgeOut]
    # The few edges that close it (cut them and it is gone), the lightest first.
    breaks: list[CycleEdgeOut]


class CyclesOut(BaseModel):
    level: Level
    sha: str
    cycles: list[CycleOut]


class DsmOut(CyclesOut):
    order: list[DsmNodeOut]
    cells: list[DsmCellOut]


def _cycles_out(found: list[Cycle]) -> list[CycleOut]:
    return [
        CycleOut(
            nodes=list(c.nodes),
            edges=[CycleEdgeOut(source=a, target=b, weight=w) for a, b, w in c.edges],
            breaks=[CycleEdgeOut(source=a, target=b, weight=w) for a, b, w in c.breaks],
        )
        for c in found
    ]


@router.get("/deps/dsm", response_model=DsmOut)
async def deps_dsm(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    level: Level = "module",
    types: Annotated[str | None, Query(max_length=500)] = None,
    min_conf: Annotated[float, Query(ge=0, le=1)] = MIN_CONF,
    ref: RefParam = None,
) -> DsmOut:
    """The level as a dependency structure matrix, with its cycles and coupling metrics."""
    await limit_graph(request, redis, access)
    snapshot, _, _, shown = await _level(
        request, db, repo, level=level, types=types, min_conf=min_conf, ref=ref
    )
    if len(shown.nodes) > MAX_DSM:
        raise InvalidError(
            f"Too many {level}s ({len(shown.nodes)}) to draw as a matrix: pick a coarser level.",
            code="dsm_too_large",
        )
    found = await _cycles(request, snapshot, shown)
    matrix = await asyncio.to_thread(dsm, shown, found)
    metrics = coupling(shown)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    return DsmOut(
        level=level,
        sha=snapshot.commit_sha,
        cycles=_cycles_out(found),
        order=[
            DsmNodeOut(
                id=key,
                label=shown.nodes[key].label,
                layer=shown.nodes[key].layer,
                size=shown.nodes[key].size,
                afferent=metrics[key].afferent,
                efferent=metrics[key].efferent,
                instability=metrics[key].instability,
            )
            for key in matrix.order
        ],
        cells=[DsmCellOut(row=r, col=c, count=n, cycle=y) for r, c, n, y in matrix.cells],
    )


@router.get("/deps/cycles", response_model=CyclesOut)
async def deps_cycles(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    level: Level = "module",
    types: Annotated[str | None, Query(max_length=500)] = None,
    min_conf: Annotated[float, Query(ge=0, le=1)] = MIN_CONF,
    ref: RefParam = None,
) -> CyclesOut:
    """The level's cycles, the largest first, each with the edges that close it."""
    await limit_graph(request, redis, access)
    snapshot, _, _, shown = await _level(
        request, db, repo, level=level, types=types, min_conf=min_conf, ref=ref
    )
    found = await _cycles(request, snapshot, shown)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    return CyclesOut(level=level, sha=snapshot.commit_sha, cycles=_cycles_out(found))


class DeclaredOut(BaseModel):
    manifest: str
    root: str
    spec: str | None
    version: str | None
    dev: bool


class UsedInOut(BaseModel):
    path: str
    imports: int


class PackageOut(BaseModel):
    ecosystem: str  # "pypi" or "npm"
    name: str
    versions: list[str]  # locked
    dev: bool  # declared for development only
    declared: list[DeclaredOut]
    files: int  # the files importing it
    imports: int
    used_in: list[UsedInOut]  # the files importing it most


class PackagesOut(BaseModel):
    sha: str
    inventoried: bool
    packages: list[PackageOut]
    undeclared: list[PackageOut]  # imported, declared nowhere


def _package_out(found: PackageInUse) -> PackageOut:
    return PackageOut(
        ecosystem=found.ecosystem,
        name=found.name,
        versions=found.versions,
        dev=found.dev,
        declared=[
            DeclaredOut(
                manifest=str(d.get("manifest", "")),
                root=str(d.get("root", ".")),
                spec=cast("str | None", d.get("spec")),
                version=cast("str | None", d.get("version")),
                dev=bool(d.get("dev")),
            )
            for d in found.declared
        ],
        files=len(found.usage.files),
        imports=found.usage.imports,
        used_in=[
            UsedInOut(path=path, imports=n)
            for path, n in sorted(found.usage.files.items(), key=lambda x: (-x[1], x[0]))[:USED_IN]
        ],
    )


@router.get("/deps/packages", response_model=PackagesOut)
async def deps_packages(
    *,
    request: Request,
    response: Response,
    access: OrgAccessDep,
    repo: RepoDep,
    db: DbSession,
    redis: RedisClient,
    ref: RefParam = None,
) -> PackagesOut:
    """The third-party packages: declared (with versions) and used (by which files)."""
    await limit_graph(request, redis, access)
    snapshot, graph = await _graph(request, db, repo, ref)
    summary = snapshot.summary or {}
    listed = cast("list[object]", summary.get("packages") or [])
    declared = [cast("dict[str, Any]", d) for d in listed if isinstance(d, dict)]
    used = await asyncio.to_thread(usage, graph)
    found, undeclared = packages_in_use(declared, used)
    response.headers[COMMIT_HEADER] = snapshot.commit_sha
    return PackagesOut(
        sha=snapshot.commit_sha,
        inventoried="packages" in summary,
        packages=[_package_out(p) for p in found],
        undeclared=[_package_out(p) for p in undeclared],
    )
