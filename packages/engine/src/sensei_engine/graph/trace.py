"""``trace_feature`` (3.12): one call that follows a feature across the layers, from where it
starts (a component, a route, a symbol) down to the tables it reads and writes, each hop with
the edge that proves it and how sure that edge is (engine §12, flow F8).

Over a snapshot's graph in memory (:class:`~sensei_engine.graph.snapshot.GraphSnapshot`), along
the edges a request travels: ``RENDERS``, ``USES_HOOK``, ``CALLS`` and ``INJECTS`` in the code,
``ISSUES_REQUEST`` (code → client call), ``TARGETS`` (client call → endpoint), ``HANDLED_BY``
(endpoint → handler), ``READS``/``WRITES`` (code → table). A function's own nested functions
(the callbacks a hook hands to ``useMutation``, a handler's closures) are part of it: their
edges are followed from it, each with its own evidence. A method that does nothing the trace
follows (an interface's, an abstract one) steps to the override of a subclass (``IMPLEMENTS``,
as sure as the class inherits), so a service called through its interface reaches its default
implementation.

The search is a beam, bounded: at most :data:`MAX_DEPTH` hops, :data:`MAX_FANOUT` ways on from
any node, edges at least ``min_conf``; a node is on a path once, and library code (externals) is
never on one. Ways on are tried nearest the goal first (the tables, or ``to``: distances from a
walk back from them), then those of the same feature (a word of the entry's name in theirs:
``Login`` → ``/login/access-token``, where a hook serves several features), then surest. The
paths that reach the goal win; then those with fewer detours through injected dependencies
(``INJECTS``: the session, the current user, which every handler has), those more of the
entry's feature, the surer (the product of their edges' confidence), those covering more layers
and the shorter. Sureness comes before layers: a handler that writes its table itself beats a
path on through a helper that writes another one (an audit row, a default folder) only to
cover the service layer. The top
:data:`TOP_PATHS` distinct ones are returned, each with its **breaks**: where it stops short of
the goal, and why (a client call no endpoint matches, an endpoint with no handler in the
repository, code that calls nothing further).
"""

from __future__ import annotations

import bisect
import heapq
import re
import weakref
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import prod
from typing import Literal

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.link.fastapi import normalize_path

MAX_DEPTH = 8
MAX_FANOUT = 8
TOP_PATHS = 3
BEAM_WIDTH = 256  # partial paths kept per depth
MAX_ENTRIES = 8  # start nodes an ambiguous entry fans out to

TRACE_EDGES = frozenset({
    EdgeType.RENDERS, EdgeType.USES_HOOK, EdgeType.CALLS, EdgeType.ISSUES_REQUEST,
    EdgeType.TARGETS, EdgeType.HANDLED_BY, EdgeType.INJECTS, EdgeType.READS, EdgeType.WRITES,
})  # fmt: skip
DATA_KINDS = frozenset({NodeKind.TABLE.value})
# Never on a path: what lies outside the repository.
OUTSIDE_KINDS = frozenset({NodeKind.EXTERNAL_SYMBOL.value, NodeKind.PACKAGE.value})
# Words too common in names to say which feature a node is part of.
STOP_WORDS = frozenset({
    "api", "app", "backend", "frontend", "src", "index", "page", "pages", "route", "routes",
    "component", "components", "service", "services", "client", "hook", "hooks", "use", "get",
    "set", "the", "and", "for", "with", "from", "handler", "util", "utils",
})  # fmt: skip
# What can hold nested functions whose edges are its own.
CALLABLE_KINDS = frozenset({
    NodeKind.FUNCTION.value, NodeKind.METHOD.value, NodeKind.COMPONENT.value, NodeKind.HOOK.value,
})  # fmt: skip
# An entry given as a file starts from what in it a feature begins with, best first.
ENTRY_KINDS = (NodeKind.COMPONENT, NodeKind.HOOK, NodeKind.FUNCTION, NodeKind.METHOD)

Layer = Literal["ui", "state", "client", "route", "handler", "service", "db", "other"]
EntryKind = Literal["auto", "ui", "route", "symbol"]


class TraceError(ValueError):
    """The entry (or ``to``) names nothing in the graph."""


@dataclass(frozen=True, slots=True)
class TraceHop:
    node: Node
    layer: Layer
    edge: Edge | None  # how it was reached (None: the entry)
    edge_type: EdgeType | None = None  # the edge's, or IMPLEMENTS for an override step
    alternatives: int = 1  # the targets its call site has (2+: ambiguous)


@dataclass(frozen=True, slots=True)
class TraceBreak:
    after_hop: int  # the index of the hop it stops after
    reason: str
    node: str  # that hop's key


@dataclass(slots=True)
class TracePath:
    hops: list[TraceHop]
    confidence: float  # the product of its edges'
    reaches_goal: bool
    breaks: list[TraceBreak] = field(default_factory=list[TraceBreak])

    @property
    def layers(self) -> list[Layer]:
        """The layers it passes through, in order, each once."""
        seen: list[Layer] = []
        for hop in self.hops:
            if hop.layer != "other" and hop.layer not in seen:
                seen.append(hop.layer)
        return seen


@dataclass(slots=True)
class TraceResult:
    entries: list[Node]  # where the paths start (several when the entry is ambiguous)
    goal: Node | None  # ``to``'s node (None: the data layer)
    paths: list[TracePath]


@dataclass(frozen=True, slots=True)
class _Step:
    there: int
    edge: Edge
    edge_type: EdgeType
    alternatives: int


class _Index:
    """What a trace reads of one graph, built once per graph: node positions, and each node's
    edges the trace follows (the surest per neighbour and type), out and in."""

    def __init__(self, graph: GraphSnapshot) -> None:
        self.graph = graph
        self.position = {node.symbol_key: i for i, node in enumerate(graph.nodes)}
        self.out: list[list[tuple[int, Edge]]] = [[] for _ in graph.nodes]
        self.into: list[list[tuple[int, Edge]]] = [[] for _ in graph.nodes]
        self.inherits_into: list[list[tuple[int, Edge]]] = [[] for _ in graph.nodes]
        for a, b, i in graph.graph.weighted_edge_list():
            edge = graph.edges[i]
            if edge.type in TRACE_EDGES:
                self.out[a].append((b, edge))
                self.into[b].append((a, edge))
            elif edge.type in (EdgeType.INHERITS, EdgeType.IMPLEMENTS):
                self.inherits_into[b].append((a, edge))
            elif edge.type is EdgeType.MAPS_TO:
                self.into[b].append((a, edge))  # only to find a table's model
        # A callable's nested callables: the keys under its own ("f()." → "f().g().").
        keys = sorted(range(len(graph.nodes)), key=lambda i: graph.nodes[i].symbol_key)
        ordered = [graph.nodes[i].symbol_key for i in keys]
        self.nested: dict[int, list[int]] = {}
        for i, node in enumerate(graph.nodes):
            if node.kind not in CALLABLE_KINDS or not node.symbol_key.endswith(")."):
                continue
            start = bisect.bisect_right(ordered, node.symbol_key)
            inner: list[int] = []
            for at in range(start, len(ordered)):
                if not ordered[at].startswith(node.symbol_key):
                    break
                if graph.nodes[keys[at]].kind in CALLABLE_KINDS:
                    inner.append(keys[at])
            if inner:
                self.nested[i] = inner
        # And the other way: the callables a nested one is part of (whose edges it lends).
        self.enclosing: dict[int, list[int]] = {}
        for outer, inner in self.nested.items():
            for i in inner:
                self.enclosing.setdefault(i, []).append(outer)


_INDEXES: weakref.WeakKeyDictionary[GraphSnapshot, _Index] = weakref.WeakKeyDictionary()


def _index(graph: GraphSnapshot) -> _Index:
    held = _INDEXES.get(graph)
    if held is None:
        held = _INDEXES[graph] = _Index(graph)
    return held


# A boundary node's layer is its kind's; a definition's its kind's, else its file's lane.
KIND_LAYERS: dict[str, Layer] = {
    NodeKind.API_ENDPOINT.value: "route",
    NodeKind.HTTP_CLIENT_CALL.value: "client",
    NodeKind.TABLE.value: "db",
    NodeKind.ORM_MODEL.value: "db",
    NodeKind.COLUMN.value: "db",
    NodeKind.SQL_STATEMENT.value: "db",
    NodeKind.HOOK.value: "state",
    NodeKind.COMPONENT.value: "ui",
}
LANE_LAYERS: dict[str, Layer] = {"ui": "ui", "api": "handler", "svc": "service", "db": "db"}


def layer_of(node: Node, reached_by: EdgeType | None) -> Layer:
    """A hop's layer: what an endpoint is handled by ``handler``; a boundary, a table, a hook or
    a component by its kind; anything else by its file's lane."""
    if reached_by is EdgeType.HANDLED_BY:
        return "handler"
    return KIND_LAYERS.get(node.kind) or LANE_LAYERS.get(node.lane, "other")


def _override(index: _Index, here: int) -> list[_Step]:
    """A method with nothing to follow steps to the same method of the classes inheriting its
    class (``py a.py/Base#run().`` → ``py b.py/Impl#run().``)."""
    node = index.graph.nodes[here]
    if node.kind != NodeKind.METHOD or "#" not in node.symbol_key:
        return []
    owner, _, member = node.symbol_key.rpartition("#")
    parent = index.position.get(f"{owner}#")
    if parent is None:
        return []
    found = [
        (override, inherits)
        for child, inherits in index.inherits_into[parent]
        if (override := index.position.get(f"{index.graph.nodes[child].symbol_key}{member}"))
        is not None
        and override != here
    ]
    return [_Step(there, edge, EdgeType.IMPLEMENTS, len(found)) for there, edge in found]


def _steps(index: _Index, here: int, min_conf: float) -> list[_Step]:
    """The ways on from ``here``: each neighbour once, by its surest edge, with how many
    targets its call site has (:func:`_site`: a call two endpoints match, a method name several
    classes have, is ambiguous; several calls, or a handler's dependencies, aren't)."""
    nodes = index.graph.nodes
    best: dict[int, Edge] = {}
    by_site: dict[tuple[EdgeType, str, int | None], int] = {}
    own = {here, *index.nested.get(here, ())}
    for there, edge in (e for at in own for e in index.out[at]):
        if there in own or edge.confidence < min_conf or nodes[there].kind in OUTSIDE_KINDS:
            continue
        if (site := _site(edge)) is not None:
            by_site[site] = by_site.get(site, 0) + 1
        held = best.get(there)
        if held is None or edge.confidence > held.confidence:
            best[there] = edge
    steps = [
        _Step(t, e, e.type, by_site[site] if (site := _site(e)) is not None else 1)
        for t, e in best.items()
    ]
    if not steps:
        steps = [s for s in _override(index, here) if s.edge.confidence >= min_conf]
    return steps


# Edges one place in the code may have many of, each certain: a signature's dependencies, the
# tables a query joins. They are never alternatives to each other.
MANY_PER_SITE = frozenset({EdgeType.INJECTS, EdgeType.READS, EdgeType.WRITES})


def _site(edge: Edge) -> tuple[EdgeType, str, int | None] | None:
    """The call site an edge is one of the possible targets of (its type and evidence line), or
    None when it can't be one of several: the kinds a site has many of, a call the resolver was
    sure of (only ``name_ambiguous`` calls are guesses among candidates), no evidence."""
    if edge.evidence_path is None or edge.type in MANY_PER_SITE:
        return None
    if edge.type is EdgeType.CALLS and edge.provenance is not Provenance.NAME_AMBIGUOUS:
        return None
    return (edge.type, edge.evidence_path, edge.evidence_line)


def _distances(index: _Index, goals: Sequence[int], min_conf: float) -> dict[int, int]:
    """How many hops each node is from a goal, walking the trace's edges back (up to the depth
    a path may have). A callable is as near as the nearest of its nested ones: their edges are
    followed from it."""
    found = dict.fromkeys(goals, 0)
    queue = deque(goals)
    while queue:
        here = queue.popleft()
        depth = found[here]
        for outer in index.enclosing.get(here, ()):
            if outer not in found or found[outer] > depth:
                found[outer] = depth
                queue.appendleft(outer)
        if depth >= MAX_DEPTH:
            continue
        for there, edge in index.into[here]:
            if edge.type is EdgeType.MAPS_TO or edge.confidence < min_conf:
                continue
            if there not in found:
                found[there] = depth + 1
                queue.append(there)
    return found


def resolve_entry(graph: GraphSnapshot, entry: str, kind: EntryKind = "auto") -> list[Node]:
    """What ``entry`` names: a node's key; a route (``POST /api/v1/items/``); a file (what in it
    a feature starts from: its components, else hooks, functions, methods); or a name or
    qualified name (every definition of it, up to :data:`MAX_ENTRIES`)."""
    text = entry.strip()
    if not text:
        raise TraceError("Say where the trace starts.")
    if kind in ("auto", "symbol") and (node := graph.node(text)) is not None:
        return [node]
    parts = text.split(maxsplit=1)
    if kind in ("auto", "route") and len(parts) == 2 and parts[0].isalpha():
        method, path = parts[0].upper(), parts[1]
        if path.startswith("/") and (
            node := graph.node(f"api_endpoint {method} {normalize_path(path)}")
        ):
            return [node]
        if kind == "route":
            raise TraceError(f"No endpoint {method} {path} in this commit.")
    nodes = graph.nodes
    if kind in ("auto", "ui"):
        in_file = [n for n in nodes if n.path == text and n.kind in ENTRY_KINDS]
        if in_file:
            best = min(ENTRY_KINDS.index(NodeKind(n.kind)) for n in in_file)
            chosen = [n for n in in_file if ENTRY_KINDS.index(NodeKind(n.kind)) == best]
            return sorted(chosen, key=lambda n: n.start_line)[:MAX_ENTRIES]
    named = [
        n
        for n in nodes
        if n.path is not None and n.kind in ENTRY_KINDS and text in (n.qualified_name, n.name)
    ]
    if named:
        return sorted(named, key=lambda n: (n.qualified_name != text, n.symbol_key))[:MAX_ENTRIES]
    raise TraceError(f"Nothing in this commit's graph is called {text!r}.")


def _words(text: str) -> set[str]:
    """The words of a name or a path: camelCase, snake_case, kebab-case and segments split."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {
        word
        for word in re.split(r"[^A-Za-z0-9]+", spaced.lower())
        if len(word) >= 3 and word not in STOP_WORDS and not word.isdigit()
    }


def _shares(words: set[str], node: Node) -> bool:
    """Whether ``node`` is named with one of ``words`` (or a longer form of one: sign, signup)."""
    for word in _words(node.name):
        for other in words:
            if word == other or (min(len(word), len(other)) >= 4 and word.startswith(other)):
                return True
            if min(len(word), len(other)) >= 4 and other.startswith(word):
                return True
    return False


@dataclass(order=True, slots=True)
class _Partial:
    sort: tuple[float, ...]
    nodes: tuple[int, ...] = field(compare=False)
    hops: tuple[TraceHop, ...] = field(compare=False)


def _score(
    hops: Sequence[TraceHop], goal_distance: int | None, feature: set[str]
) -> tuple[float, ...]:
    """Lower is better: reachable goal first, nearer it, fewer detours, more of the entry's
    feature, surer, more layers, shorter (as :func:`_best` ranks)."""
    layers = len({h.layer for h in hops if h.layer != "other"})
    confidence = prod(h.edge.confidence for h in hops if h.edge is not None)
    return (
        0 if goal_distance is not None else 1,
        goal_distance if goal_distance is not None else 0,
        _detours(hops),
        -_affinity(hops, feature),
        -confidence,
        -layers,
        len(hops),
    )


def _detours(hops: Sequence[TraceHop]) -> int:
    """How many hops go through an injected dependency rather than the code's own calls."""
    return sum(hop.edge_type is EdgeType.INJECTS for hop in hops)


def _affinity(hops: Sequence[TraceHop], feature: set[str]) -> int:
    """How many hops past the entry are named for its feature."""
    return sum(_shares(feature, hop.node) for hop in hops[1:])


def trace_feature(
    graph: GraphSnapshot,
    entry: str,
    *,
    kind: EntryKind = "auto",
    to: str | None = None,
    min_conf: float = 0.0,
) -> TraceResult:
    """The surest paths from ``entry`` (see :func:`resolve_entry`) to the data layer, or to
    ``to`` (a node's key, a route or a name, as ``entry``)."""
    index = _index(graph)
    entries = resolve_entry(graph, entry, kind)
    goal_node = resolve_entry(graph, to)[0] if to else None
    nodes = graph.nodes
    if goal_node is not None:
        goals = [index.position[goal_node.symbol_key]]
    else:
        goals = [i for i, n in enumerate(nodes) if n.kind in DATA_KINDS]
    goal_set = set(goals)
    distance = _distances(index, goals, min_conf)
    feature: set[str] = set()
    for node in entries:
        feature |= _words(node.name) | _words((node.path or "").rsplit("/", 1)[-1].split(".")[0])

    beam: list[_Partial] = []
    for node in entries:
        start = index.position[node.symbol_key]
        hops = (TraceHop(node, layer_of(node, None), None),)
        beam.append(_Partial(_score(hops, distance.get(start), feature), (start,), hops))
    done: list[TracePath] = []
    for _ in range(MAX_DEPTH):
        grown: list[_Partial] = []
        for partial in beam:
            here = partial.nodes[-1]
            steps = [s for s in _steps(index, here, min_conf) if s.there not in partial.nodes]
            steps.sort(
                key=lambda s: (
                    distance.get(s.there, MAX_DEPTH + 1),
                    not _shares(feature, nodes[s.there]),
                    s.edge_type is EdgeType.INJECTS,
                    -s.edge.confidence,
                    nodes[s.there].symbol_key,
                )
            )
            if not steps:
                done.append(_finish(partial, goal_set, "nothing_further"))
                continue
            for step in steps[:MAX_FANOUT]:
                node = nodes[step.there]
                hop = TraceHop(
                    node, layer_of(node, step.edge_type), step.edge, step.edge_type,
                    step.alternatives,
                )  # fmt: skip
                hops = (*partial.hops, hop)
                grown.append(
                    _Partial(
                        _score(hops, distance.get(step.there), feature),
                        (*partial.nodes, step.there),
                        hops,
                    )
                )
        reached = [p for p in grown if p.nodes[-1] in goal_set]
        done.extend(_finish(p, goal_set, "") for p in reached)
        beam = heapq.nsmallest(BEAM_WIDTH, [p for p in grown if p.nodes[-1] not in goal_set])
        if not beam:
            break
    done.extend(_finish(p, goal_set, "depth") for p in beam)
    return TraceResult(entries, goal_node, _best(done, feature))


def _finish(partial: _Partial, goals: set[int], why: str) -> TracePath:
    hops = list(partial.hops)
    reaches = partial.nodes[-1] in goals and len(hops) > 1
    path = TracePath(
        hops, prod(h.edge.confidence for h in hops if h.edge is not None), reaches_goal=reaches
    )
    if not reaches:
        path.breaks = _breaks(hops, why)
    return path


def _breaks(hops: Sequence[TraceHop], why: str) -> list[TraceBreak]:
    last = hops[-1]
    at = len(hops) - 1
    node = last.node
    if node.kind == NodeKind.HTTP_CLIENT_CALL:
        reason = "client call has no matching endpoint"
    elif node.kind == NodeKind.API_ENDPOINT:
        reason = "endpoint has no handler in the repository"
    elif why == "depth":
        reason = f"the trace stops at {MAX_DEPTH} hops"
    else:
        reason = f"{node.qualified_name or node.name} calls nothing the trace follows further"
    return [TraceBreak(at, reason, node.symbol_key)]


def _best(paths: Sequence[TracePath], feature: set[str]) -> list[TracePath]:
    """The top paths: reaching the goal, fewer detours, more of the entry's feature, surer,
    covering more layers, shorter (see the module); none the same nodes as another, or what
    another one continues."""
    ranked = sorted(
        paths,
        key=lambda p: (
            not p.reaches_goal,
            _detours(p.hops),
            -_affinity(p.hops, feature),
            -p.confidence,
            -len(p.layers),
            len(p.hops),
            [h.node.symbol_key for h in p.hops],
        ),
    )
    chosen: list[TracePath] = []
    seen: list[tuple[str, ...]] = []
    for path in ranked:
        keys = tuple(h.node.symbol_key for h in path.hops)
        if any(keys == other[: len(keys)] for other in seen):
            continue
        chosen.append(path)
        seen.append(keys)
        if len(chosen) == TOP_PATHS:
            break
    return chosen


def table_model(graph: GraphSnapshot, table: Node) -> Node | None:
    """The class a table is mapped from (``class -MAPS_TO-> OrmModel -MAPS_TO-> Table``)."""
    index = _index(graph)
    position = index.position.get(table.symbol_key)
    if position is None:
        return None
    nodes = graph.nodes
    for model, edge in index.into[position]:
        if edge.type is not EdgeType.MAPS_TO or nodes[model].kind != NodeKind.ORM_MODEL:
            continue
        for cls, inner in index.into[model]:
            if inner.type is EdgeType.MAPS_TO and nodes[cls].path is not None:
                return nodes[cls]
    return None
