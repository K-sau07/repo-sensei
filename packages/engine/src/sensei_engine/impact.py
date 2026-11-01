"""Impact (6.4): what a change to a symbol, or to some files, may break.

Everything that depends on the changed code, transitively (the graph's edges walked backwards:
a caller, an importer, a subclass, the endpoint a handler serves, the client call that targets
that endpoint, the page that makes it, the test that tests it), each with a **score**: the
surest way back to the change, the product of its edges' confidences, times
:data:`DECAY` for each hop, so the near and the sure come first. An endpoint and its handler, and
a client call and the endpoint it targets, are one step of the code seen from two sides
(:data:`SAME_STEP`): reaching one from the other is no hop (no decay, none of the depth).
Edges under ``min_conf`` (0.5 by default) and edges a model inferred aren't followed. The tables
the changed code reads, writes or maps to are affected too (at its score).

What is affected is sorted into **routes** (endpoints, GraphQL, RPC), **screens** (pages: the
components no other code renders, and Next.js page files), **tests**, **tables**, and the
other **symbols**, each ranked by score.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.graph.trace import pages

DECAY = 0.85  # a hop's toll on the score
MIN_CONF = 0.5
MAX_DEPTH = 8
MAX_AFFECTED = 2_000
# The edges a dependent reaches what it depends on by (walked backwards from the change).
DEPENDS_ON = frozenset(
    {
        EdgeType.CALLS, EdgeType.REFERENCES, EdgeType.IMPORTS, EdgeType.INHERITS,
        EdgeType.IMPLEMENTS, EdgeType.RENDERS, EdgeType.USES_HOOK, EdgeType.INJECTS,
        EdgeType.ISSUES_REQUEST, EdgeType.TARGETS, EdgeType.HANDLED_BY, EdgeType.RESOLVES,
        EdgeType.TESTS, EdgeType.READS, EdgeType.WRITES, EdgeType.MAPS_TO, EdgeType.CONSUMES,
    }
)  # fmt: skip
# Edges between two faces of one step (handler → endpoint, endpoint → client call): free hops, else
# a route would sit a hop past its own handler and a page's request two past it, and the depth
# would cut off routes as near as their handlers.
SAME_STEP = frozenset({EdgeType.HANDLED_BY, EdgeType.TARGETS})
DATA = frozenset({EdgeType.READS, EdgeType.WRITES, EdgeType.MAPS_TO})
ROUTE_KINDS = frozenset(
    {
        NodeKind.API_ENDPOINT.value, NodeKind.GRAPHQL_OPERATION.value,
        NodeKind.GRAPHQL_FIELD.value, NodeKind.RPC_METHOD.value,
    }
)  # fmt: skip
TABLE_KINDS = frozenset({NodeKind.TABLE.value})
LEFT_OUT = frozenset(
    {
        NodeKind.EXTERNAL_SYMBOL.value, NodeKind.PACKAGE.value, NodeKind.COLUMN.value,
        NodeKind.CLIENT_OUTCOME.value, NodeKind.HTTP_CLIENT_CALL.value,
    }
)  # fmt: skip
# Next.js pages: app/**/page.tsx (the App Router) and pages/** (the Pages Router).
NEXT_PAGE = re.compile(r"(^|/)(app/(.+/)?page|pages/.+)\.(tsx|jsx|ts|js|mdx)$")
TEST_FILE = re.compile(
    r"(^|/)(tests?/|__tests__/|test_[^/]+\.py$|[^/]+_test\.(py|go)$|[^/]+\.(test|spec)\.[jt]sx?$)"
)
Category = str  # "routes" | "screens" | "tests" | "tables" | "symbols"


@dataclass(frozen=True, slots=True)
class Affected:
    node: Node
    score: float
    distance: int  # hops from the change
    via: str | None  # the next node on its surest way to the change (None: changed itself)
    edge: EdgeType | None  # the edge it takes there


@dataclass(slots=True)
class Impact:
    changed: list[Node]
    affected: dict[Category, list[Affected]] = field(default_factory=dict[Category, list[Affected]])
    truncated: bool = False

    @property
    def heat(self) -> dict[str, float]:
        """Each affected node's score (the changed ones at 1), by key."""
        heat = {node.symbol_key: 1.0 for node in self.changed}
        for items in self.affected.values():
            for item in items:
                heat[item.node.symbol_key] = round(item.score, 4)
        return heat


def changed_nodes(
    graph: GraphSnapshot, *, symbols: Iterable[str] = (), files: Iterable[str] = ()
) -> list[Node]:
    """The nodes a change is to: the symbols named (by key), and the definitions in the files
    (what is left out of the graph's code is skipped)."""
    keys = list(dict.fromkeys(symbols))
    paths = set(files)
    found: list[Node] = []
    seen: set[str] = set()
    for key in keys:
        node = graph.node(key)
        if node is not None and key not in seen:
            seen.add(key)
            found.append(node)
    if paths:
        for node in graph.nodes:
            if node.path in paths and node.symbol_key not in seen and node.kind not in LEFT_OUT:
                seen.add(node.symbol_key)
                found.append(node)
    return found


def _follows(edge: Edge, min_conf: float) -> bool:
    return (
        edge.type in DEPENDS_ON
        and edge.confidence >= min_conf
        and edge.provenance is not Provenance.LLM_INFERRED
    )


def impact(
    graph: GraphSnapshot,
    changed: Sequence[Node],
    *,
    min_conf: float = MIN_CONF,
    max_depth: int = MAX_DEPTH,
    limit: int = MAX_AFFECTED,
) -> Impact:
    """What depends on ``changed`` (see the module), sorted and ranked."""
    index = {node.symbol_key: i for i, node in enumerate(graph.nodes)}
    starts = [index[n.symbol_key] for n in changed if n.symbol_key in index]
    best: Best = dict.fromkeys(starts, (1.0, 0, None, None))
    started = set(starts)
    queue: list[tuple[float, int, int]] = [(-1.0, 0, i) for i in starts]
    # The fewest hops each node was walked from: a node first reached the surest way, far off,
    # is walked again when a less sure way reaches it nearer (more of the depth left to go on).
    walked: dict[int, int] = {}
    queued: dict[int, int] = dict.fromkeys(starts, 0)
    while queue:
        negative, depth, here = heapq.heappop(queue)
        if walked.get(here, max_depth + 1) <= depth:
            continue
        walked[here] = depth
        for there, _, edge_index in graph.graph.in_edges(here):
            edge = graph.edges[edge_index]
            same = edge.type in SAME_STEP
            on = depth if same else depth + 1  # at the depth limit, only the free hops
            if on > max_depth or not _follows(edge, min_conf):
                continue
            if walked.get(there, max_depth + 1) <= on:
                continue
            score = -negative * edge.confidence * (1.0 if same else DECAY)
            held = best.get(there)
            if held is None or score > held[0]:
                best[there] = (score, on, here, edge.type)
            elif queued.get(there, max_depth + 1) <= on:
                continue  # neither surer nor nearer than a way already on its way
            queued[there] = min(queued.get(there, max_depth + 1), on)
            heapq.heappush(queue, (-score, on, there))
    _touched_tables(graph, starts, best, min_conf)
    items = [
        Affected(
            graph.nodes[i], score, depth, graph.nodes[via].symbol_key if via is not None else None,
            edge_type,
        )
        for i, (score, depth, via, edge_type) in best.items()
        if i not in started and graph.nodes[i].kind not in LEFT_OUT
    ]  # fmt: skip
    items.sort(key=lambda a: (-a.score, a.distance, a.node.symbol_key))
    truncated = len(items) > limit
    items = items[:limit]
    screens = pages(graph)
    result = Impact(list(changed), {name: [] for name in CATEGORIES}, truncated)
    for item in items:
        result.affected[category_of(item.node, screens)].append(item)
    return result


Best = dict[int, tuple[float, int, int | None, EdgeType | None]]


def _touched_tables(
    graph: GraphSnapshot, starts: Sequence[int], best: Best, min_conf: float
) -> None:
    """The tables the changed code touches: read, written or mapped to, directly or through
    its ORM models (code → model → table), added to ``best``."""
    started = set(starts)
    frontier = [(i, 1.0, i) for i in starts]
    for depth in (1, 2):
        following: list[tuple[int, float, int]] = []
        for here, carried, _ in frontier:
            for _, there, edge_index in graph.graph.out_edges(here):
                edge = graph.edges[edge_index]
                if edge.type not in DATA or not _follows(edge, min_conf):
                    continue
                score = carried * edge.confidence * DECAY
                if graph.nodes[there].kind in TABLE_KINDS:
                    held = best.get(there)
                    if held is None or (there not in started and score > held[0]):
                        best[there] = (score, depth, here, edge.type)
                else:
                    following.append((there, score, here))
        frontier = following


CATEGORIES: tuple[Category, ...] = ("routes", "screens", "tests", "tables", "symbols")


def category_of(node: Node, screens: Collection[str]) -> Category:
    if node.kind in ROUTE_KINDS:
        return "routes"
    if node.kind in TABLE_KINDS:
        return "tables"
    if node.kind == NodeKind.TEST.value or (node.path and TEST_FILE.search(node.path)):
        return "tests"
    if node.symbol_key in screens or (
        node.kind == NodeKind.COMPONENT.value and node.path and NEXT_PAGE.search(node.path)
    ):
        return "screens"
    return "symbols"
