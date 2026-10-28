"""The graph in memory (3.6): neighbours by direction, depth, type and confidence; the surest
path; dependents; personalized PageRank around seeds, over the code only; each node's lane; and
the artifact read back as it was written."""

from __future__ import annotations

import zlib

import orjson
import pytest

from sensei_core.models import EdgeType, Provenance
from sensei_engine.graph.snapshot import (
    CHUNK_ROWS,
    CODE_NODE_BYTES,
    MAX_DEPTH,
    NODE_BYTES,
    Edge,
    GraphSnapshot,
    Hop,
    Node,
    lane,
    neighbourhood,
)

CALLS, IMPORTS, INHERITS = EdgeType.CALLS, EdgeType.IMPORTS, EdgeType.INHERITS
SURE, GUESS = Provenance.IMPORT_RESOLVED, Provenance.NAME_UNIQUE


def _node(key: str, kind: str = "function", path: str | None = "app/a.py") -> Node:
    return Node(key, kind, key, key, path, 1, 2, "python")


def _edge(kind: EdgeType = CALLS, conf: float = 0.9) -> Edge:
    return Edge(kind, conf, SURE if conf >= 0.9 else GUESS, "app/a.py", 3)


# route -> create -> save -> commit, create -> log (a guess), route -> save (a guess),
# Model <- Base (inherits), mod imports mod2; ext is an import's external.
KEYS = ("route", "create", "save", "commit", "log", "Model", "Base", "mod", "mod2", "ext")


def _graph() -> GraphSnapshot:
    nodes = [_node(key) for key in KEYS[:5]]
    nodes += [_node("Model", "class"), _node("Base", "class"), _node("mod", "module")]
    nodes += [_node("mod2", "module"), _node("ext", "external_symbol", None)]
    at = {key: i for i, key in enumerate(KEYS)}
    edges = [
        ("route", "create", _edge()),
        ("create", "save", _edge()),
        ("save", "commit", _edge(conf=0.7)),
        ("create", "log", _edge(conf=0.4)),
        ("route", "save", _edge(conf=0.2)),
        ("Model", "Base", _edge(INHERITS)),
        ("mod", "mod2", _edge(IMPORTS)),
        ("create", "ext", _edge(conf=0.9)),
    ]
    return GraphSnapshot(nodes, [(at[a], at[b], edge) for a, b, edge in edges], version="v1")


def _keys(hops: list[Hop]) -> list[tuple[str, int]]:
    return [(hop.node.symbol_key, hop.depth) for hop in hops]


# ---------------------------------------------------------------------- neighbours


def test_neighbours_nearest_first_each_once_by_its_surest_edge() -> None:
    graph = _graph()
    hops, truncated = graph.neighbors("route", depth=2)
    assert not truncated
    # save is one hop away (by the guess) before it is two (surely): nearest wins.
    # Each depth's surest first.
    assert _keys(hops) == [("create", 1), ("save", 1), ("ext", 2), ("commit", 2), ("log", 2)]
    assert [hop.via for hop in hops] == ["route", "route", "create", "save", "create"]
    assert hops[1].edge.confidence == 0.2


def test_neighbours_by_direction_type_confidence_and_limit() -> None:
    graph = _graph()
    callers, _ = graph.neighbors("save", direction="in")
    assert _keys(callers) == [("create", 1), ("route", 1)]
    assert {hop.direction for hop in callers} == {"in"}
    both, _ = graph.neighbors("save", direction="both")
    assert _keys(both) == [("create", 1), ("commit", 1), ("route", 1)]
    sure, _ = graph.neighbors("route", depth=3, min_conf=0.5)
    assert _keys(sure) == [("create", 1), ("ext", 2), ("save", 2), ("commit", 3)]
    assert graph.neighbors("Model", edge_types={CALLS}) == ([], False)
    assert _keys(graph.neighbors("Model", edge_types={INHERITS})[0]) == [("Base", 1)]
    cut, truncated = graph.neighbors("route", depth=2, limit=3)
    assert truncated
    assert len(cut) == 3
    assert graph.neighbors("nowhere") == ([], False)
    deep, _ = graph.neighbors("route", depth=MAX_DEPTH + 5)
    assert max(hop.depth for hop in deep) <= MAX_DEPTH


# ---------------------------------------------------------------------- paths and dependents


def test_the_surest_path_not_the_shortest() -> None:
    graph = _graph()
    path = graph.shortest_path("route", "save")
    assert path is not None
    # 0.9 * 0.9 = 0.81 through create beats the direct guess at 0.2.
    assert [(hop.via, hop.node.symbol_key) for hop in path] == [
        ("route", "create"),
        ("create", "save"),
    ]
    assert [hop.depth for hop in path] == [1, 2]
    direct = graph.shortest_path("route", "save", edge_types={CALLS}, min_conf=0.1)
    assert direct is not None
    assert len(direct) == 2
    assert graph.shortest_path("route", "commit", min_conf=0.8) is None  # save -> commit is 0.7
    assert graph.shortest_path("save", "route") is None  # against the edges
    assert graph.shortest_path("route", "route") is None
    assert graph.shortest_path("route", "nowhere") is None


def test_dependents_nearest_first() -> None:
    graph = _graph()
    first, *then = graph.reverse_closure("commit")
    assert (first, set(then)) == ("save", {"create", "route"})
    assert graph.reverse_closure("commit", max_nodes=1) == ["save"]
    assert graph.reverse_closure("commit", min_conf=0.8) == []
    assert graph.reverse_closure("nowhere") == []


# ---------------------------------------------------------------------- pagerank


def test_pagerank_ranks_the_seeds_surroundings_over_the_code_only() -> None:
    graph = _graph()
    ranked = graph.personalized_pagerank({"create": 1.0})
    keys = [key for key, _ in ranked]
    assert "create" not in keys  # the seeds are left out
    assert "ext" not in keys  # an external isn't a definition
    assert {"route", "save", "log", "commit"} <= set(keys)
    score = dict(ranked)
    assert score["save"] > score["commit"]  # one hop beats two
    assert score["save"] > score["log"]  # a sure edge beats a guess
    assert all(s > 0 for s in score.values())
    assert keys == sorted(keys, key=lambda key: -score[key])


def test_pagerank_weighs_its_seeds_and_stays_within_reach() -> None:
    graph = _graph()
    toward_commit = dict(graph.personalized_pagerank({"save": 1.0, "log": 0.01}))
    toward_log = dict(graph.personalized_pagerank({"save": 0.01, "log": 1.0}))
    assert toward_commit["commit"] > toward_log["commit"]
    near = graph.personalized_pagerank({"create": 1.0}, hops=1)
    assert {key for key, _ in near} == {"save", "route", "log"}  # commit is two hops away
    assert graph.personalized_pagerank({"create": 1.0}, limit=1)[0][0] == "save"
    assert graph.personalized_pagerank({"Base": 1.0})[0][0] == "Model"
    assert graph.personalized_pagerank({"nowhere": 1.0, "ext": 1.0}) == []
    assert graph.personalized_pagerank({"create": 0.0}) == []


def test_pagerank_scores_land_on_their_own_nodes() -> None:
    """The local subgraph numbers its nodes its own way: a seed's weight and each score must
    still be its node's (a fan out of the seed's one callee, its leaves alike)."""
    keys = ["leaf_b", "seed", "a", "leaf_c", "leaf_d"]  # not in the order a walk meets them
    at = {key: i for i, key in enumerate(keys)}
    edges = [("seed", "a"), ("a", "leaf_b"), ("a", "leaf_c"), ("a", "leaf_d")]
    graph = GraphSnapshot([_node(key) for key in keys], [(at[x], at[y], _edge()) for x, y in edges])
    score = dict(graph.personalized_pagerank({"seed": 1.0}))
    assert set(score) == {"a", "leaf_b", "leaf_c", "leaf_d"}
    assert score["a"] > score["leaf_b"]
    assert score["leaf_b"] == pytest.approx(score["leaf_c"]) == pytest.approx(score["leaf_d"])


# ---------------------------------------------------------------------- lanes and the artifact


def test_a_nodes_lane_is_its_facts_or_its_files() -> None:
    assert lane("api_endpoint", None, None) == "api"
    assert lane("table", None, None) == lane("column", "db/schema.sql", None) == "db"
    assert lane("function", "app/routes/items.py", "api") == "api"
    assert lane("component", "web/src/Item.tsx", "ui") == "ui"
    assert lane("function", "app/crud.py", "service") == "svc"
    assert lane("class", "app/models.py", "db") == "db"
    assert lane("function", "tests/test_items.py", "test") == "other"
    assert lane("external_symbol", None, None) == "other"
    assert lane("function", "app/x.py", None) == "other"


def test_the_artifact_reads_back_as_written() -> None:
    graph = _graph()
    graph.nodes[0] = Node("route", "function", "route", "route", "app/a.py", 1, 2, "python", "api")
    again = GraphSnapshot.loads(graph.dumps())
    assert again.version == "v1"
    assert again.nodes == graph.nodes
    assert again.node("route") == graph.nodes[0]
    assert again.edges == [graph.edges[i] for _, _, i in graph.graph.weighted_edge_list()]
    assert _keys(again.neighbors("route", depth=2)[0]) == _keys(
        graph.neighbors("route", depth=2)[0]
    )
    blob = GraphSnapshot([], [], version="").dumps()
    assert len(GraphSnapshot.loads(blob)) == 0


def test_the_artifact_is_written_in_lines_each_parsed_alone() -> None:
    nodes = [_node(f"f{i}") for i in range(CHUNK_ROWS + 5)]
    edges = [(i, i + 1, _edge()) for i in range(CHUNK_ROWS + 3)]
    lines = zlib.decompress(GraphSnapshot(nodes, edges, version="v").dumps()).splitlines()
    assert len(lines) == 1 + 2 + 2  # the header, two lines of nodes, two of edges
    assert max(len(orjson.loads(line)) for line in lines[1:]) == CHUNK_ROWS
    again = GraphSnapshot.loads(GraphSnapshot(nodes, edges, version="v").dumps())
    assert (len(again.nodes), len(again.edges)) == (len(nodes), len(edges))


def test_an_artifact_of_another_format_or_cut_short_is_refused() -> None:
    headers: list[dict[str, object]] = [
        {"format": 99, "nodes": 0},
        {"format": 1, "nodes": [], "edges": []},  # the first format: one document
    ]
    for header in headers:
        with pytest.raises(ValueError, match="unknown graph format"):
            GraphSnapshot.loads(zlib.compress(orjson.dumps(header)))
    with pytest.raises(ValueError, match="unknown graph format"):
        GraphSnapshot.loads(zlib.compress(b""))
    blob = GraphSnapshot([_node(f"f{i}") for i in range(CHUNK_ROWS * 2)], []).dumps()
    for cut in (blob[: len(blob) // 2], blob[:-4]):
        with pytest.raises(ValueError, match="truncated"):
            GraphSnapshot.loads(cut)
    with pytest.raises(ValueError, match="after its end"):
        GraphSnapshot.loads(blob + b"more")
    with pytest.raises(ValueError, match="after its end"):
        GraphSnapshot.loads(blob + zlib.compress(b"another stream"))
    inflated = len(zlib.decompress(blob))
    assert len(GraphSnapshot.loads(blob, max_inflated=inflated)) == CHUNK_ROWS * 2
    with pytest.raises(ValueError, match="inflating past"):
        GraphSnapshot.loads(blob, max_inflated=inflated - 1)


def test_the_memory_held_counts_nodes_and_edges_and_the_code_graph() -> None:
    bare = GraphSnapshot([_node(f"f{i}") for i in range(1000)], [])
    assert bare.memory_bytes() == 1000 * (NODE_BYTES + CODE_NODE_BYTES)  # no edges: not free
    graph = _graph()
    reserved = graph.memory_bytes()
    graph.personalized_pagerank({"create": 1.0})  # builds the code graph
    assert 0 < graph.memory_bytes() <= reserved  # what was reserved for it covers it


# ---------------------------------------------------------------------- the reranker's view


def test_a_neighbourhood_names_callees_and_callers_and_the_boundaries_crossed() -> None:
    graph = _graph()
    # save is in another lane than create: the edge crosses a boundary.
    graph.nodes[2] = Node("save", "function", "save", "save", "app/db.py", 1, 2, "python", "db")
    # What it calls outside the repository is named too; a guess as sure as 0.4 is (log).
    assert neighbourhood(graph, "create") == "calls: ext, save [db], log\ncalled by: route"
    # A guess below it isn't (route -> save, 0.2).
    assert neighbourhood(graph, "save") == "calls: commit [other]\ncalled by: create [other]"
    assert neighbourhood(graph, "route", names=1) == "calls: create"
    assert neighbourhood(graph, "Model") is None  # bases aren't calls
    assert neighbourhood(graph, "nowhere") is None
