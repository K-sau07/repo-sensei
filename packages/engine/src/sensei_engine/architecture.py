"""The whole app's architecture (6.6): how a repository is built, drawn from its graph alone.

Its **modules** (the dependency view's, :mod:`sensei_engine.deps`) are the parts: the largest
:data:`MAX_PARTS` of them with code in a layer (tests, configuration and the like only when there
is little else), each in the tier most of its code is in, each listing what it is made of: its
endpoints, then its tables and models, its pages and components, its hooks and services, up to
:data:`MAX_MEMBERS` (the rest counted). The **lines** between parts are the graph's own edges
between their code, counted ("14 calls"): every one is verified, as nothing in it is a model's.
No model is asked, so a repository's architecture costs nothing to draw and draws the same
way every time; the view lays it out in its tiers (frontend, API, services, data).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from sensei_engine.deps import DEPENDS, MIN_CONF, Grouping, aggregate, group_of
from sensei_engine.diagrams import MAX_MEMBERS, DiagramSpec, Member, SpecEdge, SpecNode
from sensei_engine.graph.snapshot import GraphSnapshot

MAX_PARTS = 16
MAX_LINES = 40
MIN_PARTS_IN_LAYERS = 4  # fewer modules with code in a layer than this: the rest are shown too
# What a part's card lists first: what it serves, what it stores, what it shows, what it uses.
MEMBER_ORDER: tuple[tuple[str, str], ...] = (
    ("api_endpoint", "endpoint"),
    ("graphql_operation", "endpoint"),
    ("rpc_method", "endpoint"),
    ("table", "table"),
    ("orm_model", "model"),
    ("component", "component"),
    ("hook", "hook"),
    ("service", "service"),
    ("topic", "topic"),
)
RANK: dict[str, int] = {kind: i for i, (kind, _) in enumerate(MEMBER_ORDER)}
SAID: dict[str, str] = dict(MEMBER_ORDER)
# How a line is said by its most common edge type ("14 calls").
VERBS = {
    "calls": "calls", "imports": "imports", "references": "references", "renders": "renders",
    "uses_hook": "uses", "injects": "injects", "issues_request": "requests",
    "targets": "requests", "handled_by": "handles", "reads": "reads", "writes": "writes",
    "maps_to": "maps", "inherits": "extends", "implements": "implements",
    "publishes": "publishes", "consumes": "consumes", "tests": "tests",
}  # fmt: skip


def whole_app(graph: GraphSnapshot, roots: Grouping, title: str) -> DiagramSpec:
    """The repository's architecture as a diagram spec (see the module)."""
    dep = aggregate(graph, "module", roots, types=DEPENDS, min_conf=MIN_CONF)
    ranked = sorted(dep.nodes.values(), key=lambda node: (-node.size, node.id))
    layered = [node for node in ranked if node.layer != "other"]
    chosen = (layered if len(layered) >= MIN_PARTS_IN_LAYERS else ranked)[:MAX_PARTS]
    kept = {node.id for node in chosen}

    members: defaultdict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for node in graph.nodes:
        rank = RANK.get(node.kind)
        if rank is None:
            continue
        group = group_of(node, "module", roots)
        if group in kept:
            members[group].append((rank, node.name, SAID[node.kind]))

    nodes: list[SpecNode] = []
    for part in chosen:
        listed = sorted(set(members[part.id]))
        nodes.append(
            SpecNode(
                part.id, part.label, part.layer, "module", None, 0, 0,
                tuple(Member(name, kind) for _, name, kind in listed[:MAX_MEMBERS]),
                max(0, len(listed) - MAX_MEMBERS),
            )
        )  # fmt: skip

    lines = [
        edge
        for (src, dst), edge in dep.edges.items()
        if src in kept and dst in kept and sum(edge.types.values()) > 0
    ]
    lines.sort(key=lambda edge: (-sum(edge.types.values()), edge.src, edge.dst))
    edges: list[SpecEdge] = []
    for edge in lines[:MAX_LINES]:
        kind = Counter(edge.types).most_common(1)[0][0]
        total = sum(edge.types.values())
        verb = VERBS.get(kind, kind.replace("_", " "))
        label = f"{total} {verb}" if total > 1 else verb
        edges.append(
            SpecEdge(edge.src, edge.dst, kind, label, True, edge.confidence, "graph", None)
        )
    return DiagramSpec(title, "architecture", nodes, edges)
