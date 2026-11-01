"""The dependency graph by level (6.2): what each node of the code graph belongs to at each level,
edges counted by type between the groups, the exact lines behind an edge, and a layout that is
the same every time."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence

import pytest
import rustworkx as rx

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.deps import (
    MAX_FORCE,
    TOP,
    DepEdge,
    DepGraph,
    DepNode,
    Grouping,
    aggregate,
    breaks,
    coupling,
    cycles,
    dsm,
    evidence,
    focus,
    group_of,
    grouping,
    layout,
)
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node


def _node(key: str, path: str | None, kind: NodeKind = NodeKind.FUNCTION) -> Node:
    name = key.rsplit("/", maxsplit=1)[-1]
    return Node(key, kind.value, name, name, path, 1, 5, "python")


def _edge(
    kind: EdgeType = EdgeType.CALLS,
    line: int = 3,
    path: str = "backend/app/api/items.py",
    provenance: Provenance = Provenance.IMPORT_RESOLVED,
    confidence: float = 0.9,
) -> Edge:
    return Edge(kind, confidence, provenance, path, line)


ROUTE = "py backend/app/api/items.py/create"
ROUTE_2 = "py backend/app/api/items.py/read"
CRUD = "py backend/app/crud.py/save"
MODEL = "py backend/app/models.py/Item"
PAGE = "ts frontend/src/pages/Items.tsx/Items"
CALL = "http_client_call POST /items frontend/src/pages/Items.tsx:9"
ENDPOINT = "api_endpoint POST /items"
TABLE = "table item"
LIB = "py-package fastapi"
FILES = [
    "backend/pyproject.toml",
    "backend/app/api/items.py",
    "backend/app/crud.py",
    "backend/app/models.py",
    "frontend/package.json",
    "frontend/src/pages/Items.tsx",
    "compose.yml",
]
FRAMEWORKS = [
    {"framework": "docker-compose", "root": ".", "details": {"builds": {"api": "backend"}}},
    {"framework": "fastapi", "root": "backend"},
    {"framework": "react", "root": "frontend"},
]


@pytest.fixture
def graph() -> GraphSnapshot:
    nodes = [
        _node(ROUTE, "backend/app/api/items.py"),
        _node(ROUTE_2, "backend/app/api/items.py"),
        _node(CRUD, "backend/app/crud.py"),
        _node(MODEL, "backend/app/models.py", NodeKind.ORM_MODEL),
        _node(PAGE, "frontend/src/pages/Items.tsx", NodeKind.COMPONENT),
        _node(CALL, "frontend/src/pages/Items.tsx", NodeKind.HTTP_CLIENT_CALL),
        _node(ENDPOINT, None, NodeKind.API_ENDPOINT),
        _node(TABLE, None, NodeKind.TABLE),
        _node(LIB, None, NodeKind.PACKAGE),
    ]
    at = {n.symbol_key: i for i, n in enumerate(nodes)}
    edges = [
        (ROUTE, CRUD, _edge(line=3)),
        (ROUTE, CRUD, _edge(line=4)),  # a second call
        (ROUTE, CRUD, _edge(EdgeType.IMPORTS, line=1)),
        (ROUTE_2, CRUD, _edge(line=8, confidence=0.5)),
        (CRUD, MODEL, _edge(EdgeType.WRITES, line=2, path="backend/app/crud.py")),
        (ROUTE, ROUTE_2, _edge()),  # inside the file: no edge between groups
        (ROUTE, LIB, _edge(EdgeType.IMPORTS)),  # third-party: 6.5's
        (CRUD, MODEL, _edge(provenance=Provenance.LLM_INFERRED)),  # a model's guess
        (PAGE, CALL, _edge(EdgeType.ISSUES_REQUEST, path="frontend/src/pages/Items.tsx")),
        (CALL, ENDPOINT, _edge(EdgeType.TARGETS, path="frontend/src/pages/Items.tsx")),
        (ENDPOINT, ROUTE, _edge(EdgeType.HANDLED_BY)),
        (MODEL, TABLE, _edge(EdgeType.MAPS_TO, path="backend/app/models.py")),
        (MODEL, ROUTE, _edge(EdgeType.CONTAINS)),  # structure, not a dependency
    ]
    return GraphSnapshot(nodes, [(at[a], at[b], e) for a, b, e in edges])


def test_each_node_is_grouped_at_each_level(graph: GraphSnapshot) -> None:
    roots = grouping(FILES, FRAMEWORKS)
    assert dict(roots.packages) == {"backend": "backend", "frontend": "frontend"}
    route = graph.node(ROUTE)
    assert route is not None
    levels = ("service", "package", "module", "file")
    assert [group_of(route, level, roots) for level in levels] == [
        "backend", "backend", "backend/app/api", "backend/app/api/items.py",
    ]  # fmt: skip
    page = graph.node(PAGE)
    assert page is not None
    # An app no Compose service builds is a service of its own.
    assert dict(roots.services) == {"backend": "api", "frontend": "frontend"}
    assert group_of(page, "service", roots) == "frontend"
    endpoint = graph.node(ENDPOINT)
    assert endpoint is not None
    assert group_of(endpoint, "module", roots) == "~API endpoints"
    lib = graph.node(LIB)
    assert lib is not None
    assert group_of(lib, "file", roots) is None
    # No Compose builds: the app frameworks' roots; none of those either: the repository.
    without = grouping(FILES, FRAMEWORKS[1:])
    assert dict(without.services) == {"backend": "backend", "frontend": "frontend"}
    assert dict(grouping(["a.py"], []).services) == {TOP: "app"}
    # Two services built from one folder: the one named for it, else the first by name.
    both = [{"framework": "docker-compose", "root": ".", "details": {"builds": {
        "z": "backend", "backend": "backend", "b": "web", "a": "web",
    }}}]  # fmt: skip
    assert dict(grouping(FILES, both).services) == {"backend": "backend", "web": "a"}
    # One built from the repository's root holds every app in it.
    whole = [{"framework": "docker-compose", "root": ".", "details": {"builds": {"app": "."}}}]
    assert dict(grouping(FILES, whole + FRAMEWORKS[1:]).services) == {TOP: "app"}


def test_edges_are_counted_by_type_between_groups(graph: GraphSnapshot) -> None:
    roots = grouping(FILES, FRAMEWORKS)
    files = aggregate(graph, "file", roots)
    route_to_crud = files.edges[("backend/app/api/items.py", "backend/app/crud.py")]
    assert dict(route_to_crud.types) == {"calls": 3, "imports": 1}
    assert route_to_crud.weight == 4
    assert route_to_crud.confidence == 0.9
    assert route_to_crud.samples == [
        ("backend/app/api/items.py", 3),
        ("backend/app/api/items.py", 4),
        ("backend/app/api/items.py", 1),
    ]
    # No self edges, nothing third-party, inferred or structural.
    assert all(src != dst for src, dst in files.edges)
    assert dict(files.edges[("backend/app/crud.py", "backend/app/models.py")].types) == {
        "writes": 1
    }
    assert "~Tables" in files.nodes
    assert files.nodes["backend/app/api/items.py"].size == 2
    assert files.nodes["backend/app/api/items.py"].cluster == "backend/app/api"
    # Filters: by type and confidence.
    calls = aggregate(graph, "file", roots, types={"calls"}, min_conf=0.6)
    assert dict(calls.edges[("backend/app/api/items.py", "backend/app/crud.py")].types) == {
        "calls": 2
    }
    services = aggregate(graph, "service", roots)
    assert set(services.nodes) == {"backend", "frontend", "~API endpoints", "~Tables"}
    assert services.nodes["backend"].label == "api"
    assert services.nodes["backend"].cluster is None


def test_an_edges_evidence_is_the_codes_lines(graph: GraphSnapshot) -> None:
    roots = grouping(FILES, FRAMEWORKS)
    found, total = evidence(graph, "module", roots, "backend/app/api", "backend/app")
    assert total == 4
    assert [(e.src, e.dst, e.type, e.line) for e in found] == [
        (ROUTE, CRUD, "imports", 1),
        (ROUTE, CRUD, "calls", 3),
        (ROUTE, CRUD, "calls", 4),
        (ROUTE_2, CRUD, "calls", 8),  # the least sure last
    ]
    assert evidence(graph, "module", roots, "backend/app/api", "backend/app", limit=1)[1] == 4


def test_a_focus_is_its_neighbours_either_way(graph: GraphSnapshot) -> None:
    roots = grouping(FILES, FRAMEWORKS)
    files = aggregate(graph, "file", roots)
    near = focus(files, "backend/app/crud.py", depth=1)
    assert set(near.nodes) == {
        "backend/app/crud.py", "backend/app/api/items.py", "backend/app/models.py",
    }  # fmt: skip
    assert set(focus(files, "backend/app/crud.py", depth=2).nodes) >= {"~API endpoints", "~Tables"}
    assert focus(files, "nope").nodes == {}


def test_the_layout_is_the_same_every_time(graph: GraphSnapshot) -> None:
    roots = grouping(FILES, FRAMEWORKS)
    files = aggregate(graph, "file", roots)
    first = layout(files)
    assert first == layout(aggregate(graph, "file", roots))
    assert set(first) == set(files.nodes)
    assert all(-1.0 <= x <= 1.0 and -1.0 <= y <= 1.0 for x, y in first.values())
    assert len(set(first.values())) == len(first)  # no two on one spot


def _many(count: int) -> DepGraph:
    nodes = {f"f{i}": DepNode(f"f{i}", f"f{i}", "other", 1, f"c{i % 7}") for i in range(count)}
    return DepGraph("file", nodes, {})


def test_a_big_level_is_placed_on_a_spiral() -> None:
    big = _many(MAX_FORCE + 1)
    placed = layout(big)
    assert len(placed) == MAX_FORCE + 1
    assert placed == layout(big)
    assert layout(_many(1)) == {"f0": (0.0, 0.0)}
    assert layout(_many(0)) == {}


def test_levels_cover_the_same_code(graph: GraphSnapshot) -> None:
    roots = grouping(FILES, FRAMEWORKS)
    sizes: Sequence[int] = [
        sum(n.size for n in aggregate(graph, level, roots).nodes.values())
        for level in ("service", "package", "module", "file", "symbol")
    ]
    assert len(set(sizes)) == 1  # every code node, once at each level


def _dep(layers: dict[str, str], edges: dict[tuple[str, str], int]) -> DepGraph:
    nodes = {key: DepNode(key, key, layer, 1, None) for key, layer in layers.items()}
    made: dict[tuple[str, str], DepEdge] = {}
    for (src, dst), count in edges.items():
        made[(src, dst)] = DepEdge(src, dst, Counter({"imports": count}), 1.0)
    return DepGraph("module", nodes, made)


# A cycle of three (a → b → c → a), one of two (d ⇄ e), and f hanging off a.
CYCLIC = _dep(
    {"a": "service", "b": "service", "c": "db", "d": "ui", "e": "ui", "f": "route"},
    {("a", "b"): 2, ("b", "c"): 1, ("c", "a"): 5, ("d", "e"): 1, ("e", "d"): 1, ("f", "a"): 3},
)


def test_cycles_are_the_strongly_connected_groups() -> None:
    found = cycles(CYCLIC)
    assert [c.nodes for c in found] == [("a", "b", "c"), ("d", "e")]
    assert set(found[0].edges) == {("a", "b", 2), ("b", "c", 1), ("c", "a", 5)}
    assert found[0].breaks == (("b", "c", 1),)  # the lightest that closes it
    assert cycles(_dep({"x": "other", "y": "other"}, {("x", "y"): 1})) == []


def _acyclic_without(nodes: Sequence[str], edges: Sequence[tuple[str, str, int]]) -> bool:
    cut = {(s, d) for s, d, _ in breaks(nodes, edges)}
    graph: rx.PyDiGraph[str, None] = rx.PyDiGraph()
    at = {n: graph.add_node(n) for n in nodes}
    graph.add_edges_from_no_data([(at[s], at[d]) for s, d, _ in edges if (s, d) not in cut])
    return rx.is_directed_acyclic_graph(graph)


def test_what_closes_a_cycle_is_the_lightest_set_for_a_few_groups() -> None:
    nodes = ["app", "app/api", "app/api/routes", "app/core"]
    edges = [
        ("app", "app/api", 2), ("app", "app/core", 46), ("app/api", "app", 10),
        ("app/api", "app/api/routes", 10), ("app/api", "app/core", 9),
        ("app/api/routes", "app", 182), ("app/api/routes", "app/api", 84),
        ("app/api/routes", "app/core", 12), ("app/core", "app", 8),
    ]  # fmt: skip
    # Every order of the four tried: 20 is the least weight pointing backwards.
    assert breaks(nodes, edges) == (
        ("app", "app/api", 2), ("app/core", "app", 8), ("app/api", "app/api/routes", 10),
    )  # fmt: skip
    assert _acyclic_without(nodes, edges)


def test_what_closes_a_tangle_of_hundreds_is_a_few_of_its_edges() -> None:
    rng = random.Random(7)  # noqa: S311 - a fixed tangle, not a secret
    nodes = [f"m{i}" for i in range(300)]
    edges = list(
        {
            (nodes[a], nodes[b]): (nodes[a], nodes[b], rng.randint(1, 20))
            for a in range(300)
            for b in rng.sample(range(300), 5)
            if a != b
        }.values()
    )
    cut = breaks(nodes, edges)
    assert _acyclic_without(nodes, edges)
    assert sum(w for *_, w in cut) < 0.3 * sum(w for *_, w in edges)
    assert [w for *_, w in cut] == sorted(w for *_, w in cut)  # the lightest first


def test_the_dsm_is_ordered_by_layer_and_flags_cycle_cells() -> None:
    matrix = dsm(CYCLIC)
    assert matrix.order == ("d", "e", "f", "a", "b", "c")  # ui, route, service, db
    at = {key: i for i, key in enumerate(matrix.order)}
    cells = {(row, col): (count, cyclic) for row, col, count, cyclic in matrix.cells}
    assert cells[(at["c"], at["a"])] == (5, True)
    assert cells[(at["d"], at["e"])] == (1, True)
    assert cells[(at["f"], at["a"])] == (3, False)  # into a cycle, not in one
    assert len(matrix.cells) == len(CYCLIC.edges)


def test_coupling_metrics() -> None:
    metrics = coupling(CYCLIC)
    assert (metrics["a"].afferent, metrics["a"].efferent) == (2, 1)  # c and f in, b out
    assert metrics["a"].instability == pytest.approx(1 / 3, abs=1e-4)
    assert metrics["f"].instability == 1.0  # depends, depended on by none
    lone = coupling(_dep({"x": "other"}, {}))
    assert lone["x"].instability is None


def test_a_group_spread_over_several_parents_has_no_cluster(graph: GraphSnapshot) -> None:
    # One package at the root, over two services (the routes, the rest of the app): packages
    # and services don't nest.
    roots = Grouping({"backend/app/api": "routes", "backend/app": "core"}, {".": "app"})
    packages = aggregate(graph, "package", roots)
    assert packages.nodes["."].cluster is None
    files = aggregate(graph, "file", roots)
    assert files.nodes["backend/app/crud.py"].cluster == "backend/app"  # one module: its own
