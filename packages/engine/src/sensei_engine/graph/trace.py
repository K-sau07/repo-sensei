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
``Login`` → ``/login/access-token``, where a hook serves several features; past a request, a word
of its route's: ``POST /api/orders`` → the ``orders`` table), then surest. The paths that reach
the goal win; then those with fewer detours through injected dependencies (``INJECTS``: the
session, the current user, which every handler has), those more of the feature, the surer
(the product of their edges' confidence), those covering more layers and the shorter.
Sureness comes before layers: a handler that writes its table itself beats a path on through a
helper that writes another one (an audit row, a default folder) only to cover the service
layer. The top
:data:`TOP_PATHS` distinct ones are returned, each with its **breaks**: where it stops short of
the goal, and why (a client call no endpoint matches, an endpoint with no handler in the
repository, client code that calls nothing further: gaps; server code that calls no more of
the repository's, a DTO built or a token signed: an end; the hop limit).

**The feature** (3.24) is the entry's own words, the ``feature`` asked for (which of a
dashboard's features to follow: ``create order``), and, for an entry given as a feature in words
rather than a name (:func:`feature_entries`), those words: the endpoints whose route or handler
names its things, with the method its verb asks for (``create`` → POST), are where it starts.

A path that went down through a client call comes back up (3.22): after its deepest hop, a
**return leg** (from :attr:`TracePath.return_index`) goes back to the function that made the call
and on up the path, each with what it does with the response, in source order (``PRODUCES``
edges to :mod:`~sensei_engine.link.outcomes`' outcomes: a token stored, a page navigated to,
state set, an error shown). An outcome belongs to a hop when the call it is tied to covers the
line the hop calls on from (``const res = await login(…)``, or the ``mutationFn`` an
``onSuccess`` is beside); each is shown once. The return leg is not part of the path's
confidence, layers or ranking.
"""

from __future__ import annotations

import bisect
import functools
import heapq
import re
import weakref
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from math import prod
from typing import Literal

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.link.fastapi import normalize_path
from sensei_engine.link.outcomes import outcome_kind, outcome_tie

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
# Test code (a spec driving the app, a unit test): never where a user's journey starts.
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|e2e|spec|cypress|playwright)/|(^|/)test_[^/]*\.py$|"
    r"[._-](test|spec)\.[cm]?[jt]sx?$|_test\.(py|go)$|Tests?\.(java|kt|cs)$"
)
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
ENTRY_KINDS_SET = frozenset(kind.value for kind in ENTRY_KINDS)

Layer = Literal["ui", "state", "client", "route", "handler", "service", "db", "other"]
# Why a path stops short of the goal: a ``gap`` the graph has (a client call no endpoint matches,
# an endpoint with no handler, client code calling nothing further), an ``end`` (server code that
# calls no more of the repository's: a DTO built, a library called) or the ``limit``
# (:data:`MAX_DEPTH` hops, where it goes on).
BreakKind = Literal["gap", "end", "limit"]
EntryKind = Literal["auto", "ui", "route", "symbol"]
# down: from the entry to the data; up: from where the entry is reached (a page, an endpoint) to
# it; both: the two joined at the entry.
Direction = Literal["down", "up", "both"]


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
    kind: BreakKind = "gap"


@dataclass(slots=True)
class TracePath:
    hops: list[TraceHop]
    confidence: float  # the product of its edges'
    reaches_goal: bool
    breaks: list[TraceBreak] = field(default_factory=list[TraceBreak])
    entry_index: int = 0  # the entry's hop: what comes before it is where it is reached from
    reaches_origin: bool = False  # the hops before the entry start at a page (a UI root)
    # The first hop of the return leg (None: none): back up from the deepest hop to what the
    # code that made the request does with its response.
    return_index: int | None = None

    @property
    def layers(self) -> list[Layer]:
        """The layers it passes through on the way down, in order, each once."""
        seen: list[Layer] = []
        for hop in self.hops[: self.return_index]:
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

    def __init__(self, graph: GraphSnapshot) -> None:  # noqa: PLR0912 - one case per edge kind
        self.graph = graph
        self.position = {node.symbol_key: i for i, node in enumerate(graph.nodes)}
        self.out: list[list[tuple[int, Edge]]] = [[] for _ in graph.nodes]
        self.into: list[list[tuple[int, Edge]]] = [[] for _ in graph.nodes]
        self.inherits_into: list[list[tuple[int, Edge]]] = [[] for _ in graph.nodes]
        self.produces: dict[int, list[tuple[int, Edge]]] = {}  # a function's response outcomes
        for a, b, i in graph.graph.weighted_edge_list():
            edge = graph.edges[i]
            if edge.type is EdgeType.PRODUCES:
                self.produces.setdefault(a, []).append((b, edge))
            elif edge.type in TRACE_EDGES:
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

    def outermost(self, here: int) -> int:
        """The callable ``here`` is nested in, outermost (``here`` when it is in none)."""
        around = self.enclosing.get(here)
        if not around:
            return here
        return min(around, key=lambda i: len(self.graph.nodes[i].symbol_key))

    @functools.cached_property
    def ui_roots(self) -> frozenset[int]:
        """Where a user's journey starts: components no other code renders, calls or reaches (a
        page), and the frontend's entry modules (``main.tsx`` rendering ``<App />``). A
        component nested in another callable is that callable's, never a root."""
        reached = {
            there
            for here, edges in enumerate(self.out)
            for there, _ in edges
            if there != self.outermost(here)
        }
        return frozenset(
            i
            for i, node in enumerate(self.graph.nodes)
            if i not in reached
            and i not in self.enclosing
            and (
                node.kind == NodeKind.COMPONENT.value
                or (node.kind == NodeKind.MODULE.value and node.lane == "ui" and self.out[i])
            )
        )

    @functools.cached_property
    def from_ui(self) -> dict[int, int]:
        """How many hops each node is from a page, walking the trace's edges forward (a callable's
        nested ones lend it theirs), up to the depth a path may have."""
        found = dict.fromkeys(self.ui_roots, 0)
        queue = deque(self.ui_roots)
        while queue:
            here = queue.popleft()
            depth = found[here]
            if depth >= MAX_DEPTH:
                continue
            for at in (here, *self.nested.get(here, ())):
                for there, edge in self.out[at]:
                    if edge.type is not EdgeType.MAPS_TO and there not in found:
                        found[there] = depth + 1
                        queue.append(there)
        return found


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


# The layers a request reaches once it is on the server.
SERVER_LAYERS: frozenset[Layer] = frozenset({"route", "handler", "service", "db"})


def layer_of(node: Node, reached_by: EdgeType | None) -> Layer:
    """A hop's layer: what an endpoint is handled by ``handler``; a boundary, a table, a hook or
    a component by its kind; anything else by its file's lane (see :func:`_client_code`)."""
    if reached_by is EdgeType.HANDLED_BY:
        return "handler"
    return KIND_LAYERS.get(node.kind) or LANE_LAYERS.get(node.lane, "other")


# The code a UI file holds between the page and its request (``authService.login``).
CLIENT_CODE_KINDS = frozenset({NodeKind.FUNCTION.value, NodeKind.METHOD.value})


def _client_code(path: TracePath) -> TracePath:
    """``path`` with the functions of UI files it calls on its way to a request (an API
    client's methods: ``authService.login``) in the client's layer (``state``) rather than the
    page's; a helper the page calls on a way with no request (``cn``) stays the page's."""
    request = next(
        (i for i, hop in enumerate(path.hops) if hop.node.kind == NodeKind.HTTP_CLIENT_CALL), None
    )
    if request is None:
        return path
    hops = [
        replace(hop, layer="state")
        if i < request and hop.layer == "ui" and hop.node.kind in CLIENT_CODE_KINDS
        else hop
        for i, hop in enumerate(path.hops)
    ]
    return replace(path, hops=hops)


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
    """The ways on from ``here``: each neighbour once, by its surest edge (a table it reads and
    writes as surely, by the write), with how many
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
        if held is None or edge.confidence > held.confidence or _writes_over(edge, held):
            best[there] = edge
    steps = [
        _Step(t, e, e.type, by_site[site] if (site := _site(e)) is not None else 1)
        for t, e in best.items()
    ]
    if not steps:
        steps = [s for s in _override(index, here) if s.edge.confidence >= min_conf]
    return steps


def _writes_over(edge: Edge, held: Edge) -> bool:
    """Code that reads a table and writes it (finds a row, then saves it) changes it: its write,
    as sure as its read, is the step."""
    return (
        edge.type is EdgeType.WRITES
        and held.type is EdgeType.READS
        and edge.confidence >= held.confidence
    )


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
    a feature starts from: its components, else hooks, functions, methods); a name or qualified
    name (every definition of it, up to :data:`MAX_ENTRIES`); or, none of those, a feature in
    words (``create order``: :func:`feature_entries`)."""
    return _resolve(graph, entry, kind)[0]


def _resolve(graph: GraphSnapshot, entry: str, kind: EntryKind) -> tuple[list[Node], bool]:
    """:func:`resolve_entry`'s nodes, and whether they were found by the entry's words."""
    text = entry.strip()
    if not text:
        raise TraceError("Say where the trace starts.")
    if kind in ("auto", "symbol") and (node := graph.node(text)) is not None:
        return [node], False
    parts = text.split(maxsplit=1)
    if kind in ("auto", "route") and len(parts) == 2 and parts[0].isalpha():
        method, path = parts[0].upper(), parts[1]
        if path.startswith("/") and (
            node := graph.node(f"api_endpoint {method} {normalize_path(path)}")
        ):
            return [node], False
        if kind == "route":
            raise TraceError(f"No endpoint {method} {path} in this commit.")
    nodes = graph.nodes
    if kind in ("auto", "ui"):
        in_file = [n for n in nodes if n.path == text and n.kind in ENTRY_KINDS]
        if in_file:
            best = min(ENTRY_KINDS.index(NodeKind(n.kind)) for n in in_file)
            chosen = [n for n in in_file if ENTRY_KINDS.index(NodeKind(n.kind)) == best]
            return sorted(chosen, key=lambda n: n.start_line)[:MAX_ENTRIES], False
    named = [
        n
        for n in nodes
        if n.path is not None and n.kind in ENTRY_KINDS and text in (n.qualified_name, n.name)
    ]
    if named:
        ranked = sorted(named, key=lambda n: (n.qualified_name != text, n.symbol_key))
        return ranked[:MAX_ENTRIES], False
    if kind == "auto" and (found := feature_entries(graph, text)):
        return found, True
    raise TraceError(
        f"Nothing in this commit's graph is called {text!r}, and no endpoint or code is named "
        "for it."
    )


# What a feature's verb asks of an endpoint: the methods that do it ("create order" → POST).
VERB_METHODS: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        ("create", "add", "new", "post", "submit", "upload", "register", "signup", "place"),
        frozenset({"POST"}),
    ),
    **dict.fromkeys(("save", "store"), frozenset({"POST", "PUT", "PATCH"})),
    **dict.fromkeys(("delete", "remove", "destroy", "clear"), frozenset({"DELETE"})),
    **dict.fromkeys(
        ("update", "edit", "rename", "change", "patch", "modify", "reset"),
        frozenset({"PUT", "PATCH"}),
    ),
    **dict.fromkeys(
        ("list", "load", "view", "open", "read", "fetch", "show", "find", "search", "history"),
        frozenset({"GET"}),
    ),
}
MAX_FEATURE_ENTRIES = 3  # the endpoints (or code) a feature matching several equally starts from


def feature_entries(graph: GraphSnapshot, text: str) -> list[Node]:
    """What a feature in words starts from (``create order``, ``delete api key``): the endpoints
    whose route or handler names its things (``orders``, ``createOrder``), with a method its verb
    asks for (``create`` → POST) ranking first, as a request's spine; else the code (a page, a
    hook, a function) named with all of its words. Best first, ties together; none when nothing
    names any of them."""
    words = _words(text)
    if not words:
        return []
    verbs = {w for w in words if w in VERB_METHODS}
    things = words - verbs
    index = _index(graph)
    nodes = graph.nodes
    endpoints: list[tuple[tuple[int, ...], Node]] = []
    for i, node in enumerate(nodes):
        if node.kind != NodeKind.API_ENDPOINT.value:
            continue
        method = node.name.partition(" ")[0].upper()
        handlers: set[str] = set()
        for there, edge in index.out[i]:
            if edge.type is EdgeType.HANDLED_BY:
                handlers |= _words(nodes[there].name)
        route = _route_words(node)
        named = route | handlers
        held = sum(any(_alike(t, w) for w in named) for t in things)
        if things and held == 0:
            continue
        # No verb: what a feature named by its things alone does is show them (GET).
        asked = any(method in VERB_METHODS[v] for v in verbs) if verbs else method == "GET"
        said = sum(any(_alike(v, w) for w in handlers) for v in verbs)
        if verbs and not asked and not said:
            continue  # "delete order" is no endpoint that only creates one
        # Of the rest, the route naming least besides (``/orders/my-orders`` over
        # ``/orders/status/{status}`` for "my orders"), then the shortest.
        extra = sum(not any(_alike(w, t) for t in words) for w in route)
        segments = len([part for part in node.name.partition(" ")[2].split("/") if part])
        endpoints.append(((held, asked, said, -extra, -segments), node))
    if endpoints:
        return _top(endpoints)
    code: list[tuple[tuple[int, ...], Node]] = []
    for node in nodes:
        if node.path is None or node.kind not in ENTRY_KINDS_SET or _is_test(node):
            continue
        named = _words(node.name)
        held = sum(any(_alike(w, n) for n in named) for w in words)
        if held == len(words):
            code.append(((held, -len(named)), node))
    return _top(code)


def _top(scored: list[tuple[tuple[int, ...], Node]]) -> list[Node]:
    """The best-scored nodes, ties together (by key), at most :data:`MAX_FEATURE_ENTRIES`."""
    if not scored:
        return []
    best = max(score for score, _ in scored)
    tied = sorted((node for score, node in scored if score == best), key=lambda n: n.symbol_key)
    return tied[:MAX_FEATURE_ENTRIES]


def _words(text: str) -> set[str]:
    """The words of a name or a path: camelCase, snake_case, kebab-case and segments split."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {
        word
        for word in re.split(r"[^A-Za-z0-9]+", spaced.lower())
        if len(word) >= 3 and word not in STOP_WORDS and not word.isdigit()
    }


def _alike(word: str, other: str) -> bool:
    """The same word, or one a longer form of the other (sign, signup; order, orders)."""
    if word == other:
        return True
    return min(len(word), len(other)) >= 4 and (word.startswith(other) or other.startswith(word))


def _shares(words: set[str], node: Node) -> int:
    """How much ``node`` is named for ``words``: 2 with one of them, 1 with only a longer or
    shorter form of one (sign, signup; ``trace`` for ``traces``), 0 with none."""
    named = _words(node.name)
    if named & words:
        return 2
    return int(any(_alike(word, other) for word in named for other in words))


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
    """How much the hops past the entry are named for its feature (:func:`_shares`), or, past a
    request, for what the request is about (``POST /api/orders``: on to the ``orders`` table,
    not the ``users`` one its handler looks the caller up in on the way)."""
    count = 0
    words = feature
    for hop in hops[1:]:
        count += _shares(words, hop.node)
        if hop.node.kind == NodeKind.API_ENDPOINT:
            words = words | _route_words(hop.node)
    return count


def _path_words(hops: Sequence[TraceHop], feature: set[str]) -> set[str]:
    """The entry's feature and the words of the routes ``hops`` went through."""
    words = feature
    for hop in hops:
        if hop.node.kind == NodeKind.API_ENDPOINT:
            words = words | _route_words(hop.node)
    return words


def _route_words(endpoint: Node) -> set[str]:
    """An endpoint's resource, in words: its path's (``POST /api/orders/{id}`` → ``orders``)."""
    _, _, path = endpoint.name.partition(" ")
    return _words(re.sub(r"\{[^}]*\}|:[A-Za-z_]\w*", " ", path or endpoint.name))


def trace_feature(
    graph: GraphSnapshot,
    entry: str,
    *,
    kind: EntryKind = "auto",
    to: str | None = None,
    min_conf: float = 0.0,
    direction: Direction = "down",
    feature: str | None = None,
) -> TraceResult:
    """The surest paths from ``entry`` (see :func:`resolve_entry`) to the data layer, or to
    ``to`` (a node's key, a route or a name, as ``entry``); with ``direction`` ``up`` or
    ``both``, from where the entry is reached (see :func:`_upstream`) as well. ``feature`` (in
    words: ``create order``) says which of the entry's features to follow, where it has several
    (a dashboard listing items and placing orders); an entry found by its words
    (:func:`feature_entries`) follows them."""
    index = _index(graph)
    entries, searched = _resolve(graph, entry, kind)
    goal_node = resolve_entry(graph, to)[0] if to else None
    nodes = graph.nodes
    if goal_node is not None:
        goals = [index.position[goal_node.symbol_key]]
    else:
        goals = [i for i, n in enumerate(nodes) if n.kind in DATA_KINDS]
    goal_set = set(goals)
    distance = _distances(index, goals, min_conf)
    wanted = _wanted(entries, feature, entry if searched else None)
    beam: list[_Partial] = []
    for node in entries:
        start = index.position[node.symbol_key]
        hops = (TraceHop(node, layer_of(node, None), None),)
        beam.append(_Partial(_score(hops, distance.get(start), wanted), (start,), hops))
    done: list[TracePath] = []
    for _ in range(MAX_DEPTH):
        grown: list[_Partial] = []
        for partial in beam:
            here = partial.nodes[-1]
            steps = [s for s in _steps(index, here, min_conf) if s.there not in partial.nodes]
            words = _path_words(partial.hops, wanted)
            steps.sort(
                key=lambda s: (
                    distance.get(s.there, MAX_DEPTH + 1),
                    -_shares(words, nodes[s.there]),
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
                        _score(hops, distance.get(step.there), wanted),
                        (*partial.nodes, step.there),
                        hops,
                    )
                )
        reached = [p for p in grown if p.nodes[-1] in goal_set]
        done.extend(_finish(p, goal_set, "") for p in reached)
        beam = heapq.nsmallest(BEAM_WIDTH, [p for p in grown if p.nodes[-1] not in goal_set])
        if not beam:
            break
    # Out of hops: a way that could go on stops at the limit; one that couldn't has ended.
    done.extend(
        _finish(
            p,
            goal_set,
            "depth"
            if any(s.there not in p.nodes for s in _steps(index, p.nodes[-1], min_conf))
            else "nothing_further",
        )
        for p in beam
    )
    downs = _best(done, wanted)
    if direction == "down":
        return TraceResult(entries, goal_node, _returning(index, downs))
    ups = {
        node.symbol_key: _upstream(index, index.position[node.symbol_key], wanted, min_conf)
        for node in entries
    }
    if direction == "up":
        every = [path for found in ups.values() for path in found]
        return TraceResult(entries, goal_node, _returning(index, _best_up(every, wanted)))
    return TraceResult(entries, goal_node, _returning(index, _joined(downs, ups)))


def _wanted(entries: Sequence[Node], feature: str | None, searched: str | None) -> set[str]:
    """The feature a trace follows: its entries' own words (a name, a file's), the ``feature``
    asked for, and the words an entry was found by (``searched``)."""
    wanted: set[str] = set()
    for node in entries:
        wanted |= _words(node.name) | _words((node.path or "").rsplit("/", 1)[-1].split(".")[0])
    for words in (feature, searched):
        if words:
            wanted |= _words(words)
    return wanted


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
    name = node.qualified_name or node.name
    if node.kind == NodeKind.HTTP_CLIENT_CALL:
        return [TraceBreak(at, "client call has no matching endpoint", node.symbol_key)]
    if node.kind == NodeKind.API_ENDPOINT:
        return [TraceBreak(at, "endpoint has no handler in the repository", node.symbol_key)]
    if why == "depth":
        reason = f"the trace shows {MAX_DEPTH} hops; {name} goes on"
        return [TraceBreak(at, reason, node.symbol_key, "limit")]
    if any(hop.layer in SERVER_LAYERS for hop in hops):
        # On the server already: code that does its work without the data (a DTO built, a token
        # signed) is where this way ends, not a gap.
        reason = f"{name} calls no more of the repository's code"
        return [TraceBreak(at, reason, node.symbol_key, "end")]
    # Still in the client: the request it leads to is missing.
    reason = f"{name} calls nothing the trace follows further"
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


# ---------------------------------------------------------------- upstream


def _steps_up(index: _Index, here: int, min_conf: float) -> list[_Step]:
    """The ways back from ``here``: what reaches it, each once by its surest edge (a nested
    callable's edge counted as its outermost callable's: ``Login().handleSubmit()`` calling
    ``login`` is ``Login`` calling it), and the callable it is nested in (``CONTAINS``)."""
    nodes = index.graph.nodes
    best: dict[int, Edge] = {}
    for source, edge in index.into[here]:
        if edge.type not in TRACE_EDGES or edge.confidence < min_conf:
            continue
        there = index.outermost(source)
        if there == here or nodes[there].kind in OUTSIDE_KINDS or _is_test(nodes[there]):
            continue
        held = best.get(there)
        if held is None or edge.confidence > held.confidence:
            best[there] = edge
    steps = [_Step(there, edge, edge.type, 1) for there, edge in best.items()]
    outer = index.outermost(here)
    if outer != here and outer not in best:
        inner = nodes[here]
        line = inner.start_line if inner.start_line > 0 else None
        held = Edge(EdgeType.CONTAINS, 1.0, Provenance.IMPORT_RESOLVED, inner.path, line)
        steps.append(_Step(outer, held, EdgeType.CONTAINS, 1))
    return steps


@dataclass(order=True, slots=True)
class _Up:
    sort: tuple[float, ...]
    nodes: tuple[int, ...] = field(compare=False)  # the entry first, then back towards a page
    steps: tuple[_Step, ...] = field(compare=False)  # steps[i]: from nodes[i + 1] to nodes[i]


def _upstream(index: _Index, start: int, feature: set[str], min_conf: float) -> list[TracePath]:
    """Where ``start`` is reached from, best first: the trace's edges walked back (callers, the
    components rendering it, the endpoint handing off to it, the client call targeting that
    endpoint, ...) until a page (:attr:`_Index.ui_roots`) or nothing reaches further (an origin:
    an endpoint of a backend alone, a job, unused code). Ways back are tried nearest a page
    first, then of the entry's feature, then surest; each path ends at the entry."""
    nodes = index.graph.nodes
    near = index.from_ui
    roots = index.ui_roots

    def score(path: tuple[int, ...], steps: tuple[_Step, ...]) -> tuple[float, ...]:
        last = path[-1]
        return (
            near.get(last, MAX_DEPTH + 1),
            sum(step.edge_type is EdgeType.INJECTS for step in steps),
            -sum(_shares(feature, nodes[i]) for i in path[1:]),
            -prod(step.edge.confidence for step in steps),
            len(path),
        )

    beam = [_Up(score((start,), ()), (start,), ())]
    done: list[_Up] = []
    for _ in range(MAX_DEPTH):
        grown: list[_Up] = []
        for partial in beam:
            here = partial.nodes[-1]
            steps = [] if here in roots else _steps_up(index, here, min_conf)
            steps = [s for s in steps if s.there not in partial.nodes]
            if not steps:
                done.append(partial)
                continue
            steps.sort(
                key=lambda s: (
                    near.get(s.there, MAX_DEPTH + 1),
                    -_shares(feature, nodes[s.there]),
                    s.edge_type is EdgeType.INJECTS,
                    -s.edge.confidence,
                    nodes[s.there].symbol_key,
                )
            )
            for step in steps[:MAX_FANOUT]:
                path, taken = (*partial.nodes, step.there), (*partial.steps, step)
                grown.append(_Up(score(path, taken), path, taken))
        beam = heapq.nsmallest(BEAM_WIDTH, grown)
        if not beam:
            break
    done.extend(beam)
    return _best_up([_up_path(index, partial) for partial in done], feature)


def _up_path(index: _Index, partial: _Up) -> TracePath:
    """A walk back as a path read forward: from where it starts to the entry."""
    nodes = index.graph.nodes
    order = list(reversed(partial.nodes))
    steps = list(reversed(partial.steps))
    first = nodes[order[0]]
    hops = [TraceHop(first, layer_of(first, None), None)]
    for step, at in zip(steps, order[1:], strict=True):
        node = nodes[at]
        hops.append(TraceHop(node, layer_of(node, step.edge_type), step.edge, step.edge_type))
    return TracePath(
        hops,
        prod(step.edge.confidence for step in steps),
        reaches_goal=False,
        entry_index=len(hops) - 1,
        reaches_origin=order[0] in index.ui_roots,
    )


def _best_up(paths: Sequence[TracePath], feature: set[str]) -> list[TracePath]:
    """The top ways to the entry: from a page first, then fewer detours, more of the entry's
    feature before it (the ``Login`` page for ``/login/access-token``), surer, shorter; none the
    same nodes as another, or the tail of one. Test code is never on one (:data:`TEST_PATH`)."""
    ranked = sorted(
        paths,
        key=lambda p: (
            not p.reaches_origin,
            _detours(p.hops),
            -_affinity_up(p.hops, feature),
            -p.confidence,
            len(p.hops),
            [h.node.symbol_key for h in p.hops],
        ),
    )
    chosen: list[TracePath] = []
    seen: list[tuple[str, ...]] = []
    for path in ranked:
        keys = tuple(h.node.symbol_key for h in path.hops)
        if any(keys == other[-len(keys) :] for other in seen):
            continue
        chosen.append(path)
        seen.append(keys)
        if len(chosen) == TOP_PATHS:
            break
    return chosen


def _is_test(node: Node) -> bool:
    return node.path is not None and TEST_PATH.search(node.path) is not None


def _affinity_up(hops: Sequence[TraceHop], feature: set[str]) -> int:
    """How much the hops before the entry are named for its feature (the page most of all)."""
    return sum(_shares(feature, hop.node) for hop in hops[:-1])


def _join(up: TracePath, down: TracePath) -> TracePath:
    """``up`` (ending at the entry) and ``down`` (starting there) as one path."""
    shift = up.entry_index
    return TracePath(
        [*up.hops, *down.hops[1:]],
        up.confidence * down.confidence,
        reaches_goal=down.reaches_goal,
        breaks=[replace(b, after_hop=b.after_hop + shift) for b in down.breaks],
        entry_index=shift,
        reaches_origin=up.reaches_origin,
    )


def _joined(downs: Sequence[TracePath], ups: Mapping[str, Sequence[TracePath]]) -> list[TracePath]:
    """The top whole journeys: the best way to the entry with each way on from it, and the
    other ways to it with the best way on (a function two pages reach is shown from both)."""
    pairs: list[tuple[int, int]] = [(0, 0), (0, 1), (1, 0), (0, 2), (2, 0)]
    chosen: list[TracePath] = []
    seen: set[tuple[str, ...]] = set()
    for u, d in pairs:
        if d >= len(downs):
            continue
        down = downs[d]
        found = ups.get(down.hops[0].node.symbol_key, ())
        if u >= len(found):
            continue
        path = _join(found[u], down)
        keys = tuple(h.node.symbol_key for h in path.hops)
        if keys in seen:
            continue
        chosen.append(path)
        seen.add(keys)
        if len(chosen) == TOP_PATHS:
            break
    return chosen


# ---------------------------------------------------------------- the return leg

# An outcome's layer: where it lands (a page, the client's state).
OUTCOME_LAYERS: dict[str, Layer] = {
    "navigate": "ui", "error": "ui", "storage": "state", "state": "state",
}  # fmt: skip
# Steps that aren't a call from the hop before (its nested function, an override).
NOT_CALLS = frozenset({EdgeType.CONTAINS, EdgeType.IMPLEMENTS})


def _outcomes(index: _Index, here: int, path: str, line: int) -> list[tuple[int, Edge]]:
    """What ``here`` (and the functions nested in it) does with the response of its call on
    ``line``: the outcomes tied to lines covering it, in source order."""
    nodes = index.graph.nodes
    found: list[tuple[int, Edge]] = []
    for at in (here, *index.nested.get(here, ())):
        for there, edge in index.produces.get(at, ()):
            ties = outcome_tie(nodes[there].symbol_key) or ()
            if nodes[there].path == path and any(a <= line <= b for a, b in ties):
                found.append((there, edge))
    return sorted(found, key=lambda f: (f[1].evidence_line or 0, nodes[f[0]].symbol_key))


def _return_leg(index: _Index, path: TracePath) -> TracePath:
    """``path`` with its return leg (see the module): from the code that made its client call
    back up the path, each hop that does something with the response followed by what it does.
    A path with no client call, or whose callers do nothing with it, is as it was."""
    hops = path.hops
    calls = [
        i for i, hop in enumerate(hops) if i > 0 and hop.node.kind == NodeKind.HTTP_CLIENT_CALL
    ]
    if not calls:
        return path
    nodes = index.graph.nodes
    leg: list[TraceHop] = []
    shown: set[int] = set()
    for i in range(calls[0] - 1, -1, -1):
        hop, down = hops[i], hops[i + 1]
        edge = down.edge
        if (
            edge is None
            or down.edge_type in NOT_CALLS
            or edge.evidence_path is None
            or edge.evidence_line is None
        ):
            continue
        here = index.position.get(hop.node.symbol_key)
        if here is None:
            continue
        found = [
            (there, produced)
            for there, produced in _outcomes(index, here, edge.evidence_path, edge.evidence_line)
            if there not in shown
        ]
        if not found:
            continue
        # Back to the hop, at the line it called on from; then what it does there.
        leg.append(TraceHop(hop.node, hop.layer, edge, down.edge_type))
        for there, produced in found:
            shown.add(there)
            node = nodes[there]
            layer = OUTCOME_LAYERS.get(outcome_kind(node.symbol_key) or "", "state")
            leg.append(TraceHop(node, layer, produced, EdgeType.PRODUCES))
    if not leg:
        return path
    return TracePath(
        [*hops, *leg],
        path.confidence,
        reaches_goal=path.reaches_goal,
        breaks=path.breaks,
        entry_index=path.entry_index,
        reaches_origin=path.reaches_origin,
        return_index=len(hops),
    )


def _returning(index: _Index, paths: Sequence[TracePath]) -> list[TracePath]:
    return [_return_leg(index, _client_code(path)) for path in paths]


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
