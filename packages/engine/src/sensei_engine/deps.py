"""The dependency graph by level (6.2): the code graph seen as services, packages, modules, files or
symbols, each edge the count of the code's edges between them, by type.

What a node of the code graph belongs to, at each level:
- **symbol**: itself;
- **file**: its file;
- **module**: its file's folder (``.`` at the top);
- **package**: the deepest folder above it holding a manifest (``pyproject.toml``,
  ``package.json``, ``go.mod``...), else its top folder;
- **service**: the deepest folder a Compose service is built from; else (no Compose builds)
  the deepest root of an app framework (FastAPI, Next.js, Express...); else the repository.

A node with no file (an endpoint, a table, a topic) is grouped by its kind at every level but
the symbol one: the API's endpoints, the tables. Third-party packages and what an import names
outside the repository are left out (they are 6.5's), as are containment and definitions (the
structure the levels already are) and edges a model inferred.

Each level is laid out once per graph (:func:`layout`): a seeded force layout, the same every
time, for a level up to :data:`MAX_FORCE` nodes; a bigger one is placed cluster by cluster on a
spiral. A focus (a node and its neighbours ``depth`` hops out, either way) is a subset of its
level, drawn where the whole level puts it.
"""

from __future__ import annotations

import math
import posixpath
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import rustworkx as rx

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.graph.trace import layer_of

Level = Literal["service", "package", "module", "file", "symbol"]
LEVELS: tuple[Level, ...] = ("service", "package", "module", "file", "symbol")
TOP = "."  # the repository's top folder

# Edges that are dependencies (the levels are the structure; a model's guess isn't one).
DEPENDS = frozenset(EdgeType) - {EdgeType.CONTAINS, EdgeType.DEFINES}
OUTSIDE = frozenset({NodeKind.EXTERNAL_SYMBOL.value, NodeKind.PACKAGE.value})
MANIFESTS = frozenset(
    {
        "pyproject.toml", "setup.py", "setup.cfg", "package.json", "go.mod", "Cargo.toml",
        "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "Gemfile",
    }
)  # fmt: skip
APP_FRAMEWORKS = frozenset(
    {
        "fastapi", "django", "flask", "starlette", "next", "nuxt", "sveltekit", "express",
        "fastify", "koa", "hono", "nestjs", "react", "vue", "svelte", "angular",
    }
)  # fmt: skip
KIND_GROUPS: Mapping[str, str] = {
    NodeKind.API_ENDPOINT.value: "API endpoints",
    NodeKind.GRAPHQL_OPERATION.value: "GraphQL",
    NodeKind.GRAPHQL_FIELD.value: "GraphQL",
    NodeKind.RPC_METHOD.value: "RPC methods",
    NodeKind.TOPIC.value: "Topics",
    NodeKind.TABLE.value: "Tables",
    NodeKind.COLUMN.value: "Tables",
}
SAMPLES = 3  # evidence lines kept on an aggregated edge
MAX_FORCE = 2_000  # nodes a force layout is run for (more: the spiral)
SEED = 7


@dataclass(frozen=True, slots=True)
class Grouping:
    """The folders services and packages are rooted at (folder → name)."""

    services: Mapping[str, str]
    packages: Mapping[str, str]


def grouping(paths: Iterable[str], frameworks: Sequence[Mapping[str, Any]]) -> Grouping:
    """The service and package roots of a snapshot: its files' paths and what discovery found
    (``summary["frameworks"]``)."""
    packages: dict[str, str] = {}
    for path in paths:
        folder, name = _split(path)
        if name in MANIFESTS or name.endswith(".csproj"):
            packages[folder] = _base(folder)
    services: dict[str, str] = {}
    for found in frameworks:
        details = found.get("details")
        builds = (
            cast("dict[str, object]", details).get("builds") if isinstance(details, dict) else None
        )
        if isinstance(builds, dict):
            for name, root in cast("dict[str, object]", builds).items():
                if isinstance(root, str):
                    services[root] = str(name)
    if not services:
        for found in frameworks:
            root = found.get("root")
            if found.get("framework") in APP_FRAMEWORKS and isinstance(root, str):
                services[root] = _base(root)
    return Grouping(services or {TOP: "app"}, packages)


def _split(path: str) -> tuple[str, str]:
    folder, _, name = path.rpartition("/")
    return folder or TOP, name


def _base(folder: str) -> str:
    return "app" if folder == TOP else folder.rsplit("/", 1)[-1]


def _deepest(folder: str, roots: Mapping[str, str]) -> str | None:
    while True:
        if folder in roots:
            return folder
        if folder == TOP:
            return None
        folder = posixpath.dirname(folder) or TOP


def group_of(node: Node, level: Level, roots: Grouping) -> str | None:
    """The group ``node`` is in at ``level`` (its id there); None for what's left out."""
    if node.kind in OUTSIDE:
        return None
    if level == "symbol":
        return node.symbol_key
    if not node.path:
        return f"~{KIND_GROUPS.get(node.kind, node.kind)}"
    return _group_of_file(node.path, level, roots)


def _group_of_file(path: str, level: Level, roots: Grouping) -> str:
    if level == "file":
        return path
    folder, _ = _split(path)
    if level == "module":
        return folder
    if level == "package":
        found = _deepest(folder, roots.packages)
        return found if found is not None else folder.split("/", 1)[0]
    found = _deepest(folder, roots.services)
    return found if found is not None else TOP


def parent_level(level: Level) -> Level | None:
    index = LEVELS.index(level)
    return LEVELS[index - 1] if index > 0 else None


def label_of(group: str, level: Level, roots: Grouping, graph: GraphSnapshot) -> str:
    if group.startswith("~"):
        return group[1:]
    if level == "symbol":
        node = graph.node(group)
        return node.qualified_name or node.name if node is not None else group
    if level == "service":
        return roots.services.get(group, "app" if group == TOP else _base(group))
    if level == "package" and group in roots.packages:
        return roots.packages[group]
    return group


@dataclass(slots=True)
class DepNode:
    id: str
    label: str
    layer: str
    size: int  # the code graph's nodes in it
    cluster: str | None  # its group a level up


@dataclass(slots=True)
class DepEdge:
    src: str
    dst: str
    types: Counter[str] = field(default_factory=Counter[str])
    confidence: float = 0.0  # the surest underlying edge's
    samples: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])

    @property
    def weight(self) -> int:
        return sum(self.types.values())


@dataclass(slots=True)
class DepGraph:
    level: Level
    nodes: dict[str, DepNode]
    edges: dict[tuple[str, str], DepEdge]


def _counts(edge: Edge, types: Collection[str] | None, min_conf: float) -> bool:
    return (
        edge.type in DEPENDS
        and edge.provenance is not Provenance.LLM_INFERRED
        and edge.confidence >= min_conf
        and (types is None or edge.type.value in types)
    )


def aggregate(
    graph: GraphSnapshot,
    level: Level,
    roots: Grouping,
    *,
    types: Collection[str] | None = None,
    min_conf: float = 0.0,
) -> DepGraph:
    """The level's nodes (with their layer, size and cluster) and edges (counted by type)."""
    up = parent_level(level)
    member: list[str | None] = [group_of(node, level, roots) for node in graph.nodes]
    layers: defaultdict[str, Counter[str]] = defaultdict(Counter)
    sizes: Counter[str] = Counter()
    clusters: dict[str, str | None] = {}
    for node, group in zip(graph.nodes, member, strict=True):
        if group is None:
            continue
        sizes[group] += 1
        layers[group][layer_of(node, None)] += 1
        if group not in clusters:
            clusters[group] = group_of(node, up, roots) if up is not None else None
    nodes = {
        group: DepNode(
            group, label_of(group, level, roots, graph), _layer(layers[group]), sizes[group],
            clusters[group],
        )
        for group in sorted(sizes)
    }  # fmt: skip
    edges: dict[tuple[str, str], DepEdge] = {}
    for a, b, index in graph.graph.weighted_edge_list():
        edge = graph.edges[index]
        src, dst = member[a], member[b]
        if src is None or dst is None or src == dst or not _counts(edge, types, min_conf):
            continue
        held = edges.get((src, dst))
        if held is None:
            held = edges[(src, dst)] = DepEdge(src, dst)
        held.types[edge.type.value] += 1
        held.confidence = max(held.confidence, edge.confidence)
        if len(held.samples) < SAMPLES and edge.evidence_path and edge.evidence_line:
            held.samples.append((edge.evidence_path, edge.evidence_line))
    return DepGraph(level, nodes, dict(sorted(edges.items())))


def _layer(counts: Counter[str]) -> str:
    """The layer most of a group's code is in (``other`` only when it is all there is)."""
    ranked = sorted(
        ((n, layer) for layer, n in counts.items() if layer != "other"), key=lambda x: (-x[0], x[1])
    )
    return ranked[0][1] if ranked else "other"


def focus(dep: DepGraph, node: str, depth: int = 1) -> DepGraph:
    """``node`` and its neighbours ``depth`` hops out (either way), with the edges among them."""
    if node not in dep.nodes:
        return DepGraph(dep.level, {}, {})
    near: defaultdict[str, set[str]] = defaultdict(set)
    for src, dst in dep.edges:
        near[src].add(dst)
        near[dst].add(src)
    seen = {node}
    frontier = [node]
    for _ in range(max(1, depth)):
        frontier = sorted({n for here in frontier for n in near[here]} - seen)
        seen.update(frontier)
        if not frontier:
            break
    return DepGraph(
        dep.level,
        {key: value for key, value in dep.nodes.items() if key in seen},
        {key: e for key, e in dep.edges.items() if key[0] in seen and key[1] in seen},
    )


@dataclass(frozen=True, slots=True)
class Evidence:
    src: str  # symbol keys
    dst: str
    type: str
    confidence: float
    provenance: str
    path: str | None
    line: int | None


def evidence(
    graph: GraphSnapshot,
    level: Level,
    roots: Grouping,
    src: str,
    dst: str,
    *,
    types: Collection[str] | None = None,
    min_conf: float = 0.0,
    limit: int = 200,
) -> tuple[list[Evidence], int]:
    """The code's edges from group ``src`` to group ``dst`` at ``level`` (the import and call
    lines), surest first, at most ``limit``; and how many there are."""
    found: list[Evidence] = []
    for a, b, index in graph.graph.weighted_edge_list():
        edge = graph.edges[index]
        here, there = graph.nodes[a], graph.nodes[b]
        if not _counts(edge, types, min_conf):
            continue
        if group_of(here, level, roots) != src or group_of(there, level, roots) != dst:
            continue
        found.append(
            Evidence(
                here.symbol_key, there.symbol_key, edge.type.value, edge.confidence,
                edge.provenance.value, edge.evidence_path, edge.evidence_line,
            )
        )  # fmt: skip
    found.sort(key=lambda e: (-e.confidence, e.path or "", e.line or 0, e.src, e.dst, e.type))
    return found[:limit], len(found)


def layout(dep: DepGraph, *, seed: int = SEED) -> dict[str, tuple[float, float]]:
    """Where each node of the level is drawn, in [-1, 1]²: a seeded force layout (the same each
    time), or for more than :data:`MAX_FORCE` nodes, cluster by cluster on a spiral."""
    ids = sorted(dep.nodes)
    if not ids:
        return {}
    if len(ids) == 1:
        return {ids[0]: (0.0, 0.0)}
    if len(ids) > MAX_FORCE:
        return _spiral(dep, ids)
    at = {key: i for i, key in enumerate(ids)}
    graph: rx.PyGraph[str, float] = rx.PyGraph(multigraph=False)
    graph.add_nodes_from(ids)
    for (src, dst), edge in dep.edges.items():
        a, b = at[src], at[dst]
        weight = 1.0 + math.log1p(edge.weight)
        held = graph.get_edge_data(a, b) if graph.has_edge(a, b) else 0.0
        if graph.has_edge(a, b):
            graph.update_edge(a, b, held + weight)
        else:
            graph.add_edge(a, b, weight)
    start = _spiral(dep, ids)
    positions = rx.spring_layout(
        graph,
        pos={i: start[key] for i, key in enumerate(ids)},
        weight_fn=float,
        num_iter=100,
        seed=seed,
    )
    return _normalized({ids[i]: (float(x), float(y)) for i, (x, y) in positions.items()})


def _spiral(dep: DepGraph, ids: Sequence[str]) -> dict[str, tuple[float, float]]:
    """Clusters together, largest first, each node on one golden-angle spiral."""
    order = sorted(ids, key=lambda key: (dep.nodes[key].cluster or "", -dep.nodes[key].size, key))
    golden = math.pi * (3 - math.sqrt(5))
    count = len(order)
    return {
        key: (
            math.sqrt((i + 0.5) / count) * math.cos(i * golden),
            math.sqrt((i + 0.5) / count) * math.sin(i * golden),
        )
        for i, key in enumerate(order)
    }


def _normalized(points: Mapping[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    xs = [x for x, _ in points.values()]
    ys = [y for _, y in points.values()]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    return {
        key: (round((x - cx) * 2 / span, 5), round((y - cy) * 2 / span, 5))
        for key, (x, y) in points.items()
    }
