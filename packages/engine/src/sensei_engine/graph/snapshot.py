"""A snapshot's graph in memory (3.6): its nodes and edges in a ``rustworkx`` directed graph, for
traversals that answer in milliseconds.

:meth:`GraphSnapshot.build` reads the snapshot's nodes (definitions and the graph's own) and edges
from the database; :meth:`GraphSnapshot.dumps` writes them as the snapshot's ``graph.bin``
artifact (lines of JSON arrays, zlib-compressed: a few MB for a large repository) and
:meth:`GraphSnapshot.loads` reads it back (the API keeps graphs in memory:
:mod:`sensei_api.graphs`).

Operations (edge confidence is the weight; ``min_conf`` drops what is less sure):

- :meth:`~GraphSnapshot.neighbors`: what a node reaches (``out``), what reaches it (``in``), or
  both, up to ``depth`` 3 hops, nearest first;
- :meth:`~GraphSnapshot.shortest_path`: the most certain path from one node to another along the
  edges' direction (the product of confidences, highest);
- :meth:`~GraphSnapshot.personalized_pagerank`: a structural ranking of definitions around
  weighted seeds, over the code (definitions and their edges, both ways);
- :meth:`~GraphSnapshot.reverse_closure`: everything that depends on a node (impact, 6.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import math
import uuid
import zlib
from collections import deque
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import orjson
import rustworkx as rx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import (
    SYMBOL_KINDS,
    CodeEdge,
    CodeNode,
    EdgeType,
    NodeKind,
    Provenance,
    RepoFile,
)
from sensei_engine.search.layers import lane_of, layer_of
from sensei_engine.search.route import Lane

FORMAT = 2  # 2: rows in lines of CHUNK_ROWS, each parsed alone (1: one document)
CHUNK_ROWS = 10_000
# What an artifact may inflate to: Langflow's is 85 MB (from 6 MB); past this it isn't read.
MAX_INFLATED_BYTES = 1 << 30
ARTIFACT_NAME = "graph.bin"
MAX_DEPTH = 3
MAX_LIMIT = 500
CODE_EDGES = frozenset({
    EdgeType.CALLS, EdgeType.REFERENCES, EdgeType.INHERITS, EdgeType.IMPLEMENTS,
    EdgeType.IMPORTS, EdgeType.RENDERS, EdgeType.USES_HOOK, EdgeType.INJECTS,
})  # fmt: skip
# What "callers" and "callees" follow.
CALL_EDGES = frozenset({EdgeType.CALLS, EdgeType.USES_HOOK, EdgeType.RENDERS, EdgeType.INJECTS})
PPR_MAX_NODES = 20_000
# Memory held, for the API's budget, measured on Langflow (139k nodes, 457k edges: about 220 MB):
# a node with its strings (a definition's own, its path's and kind's shared), an edge with its
# payload and its place in the graph; the ranking's code graph (a definition, an edge each way)
# is reserved for until it is built.
NODE_BYTES = 600
EDGE_BYTES = 200
CODE_NODE_BYTES = 60
CODE_EDGE_BYTES = 100
# The lane of a graph fact by its kind (else its file's, as search hits'; else "other").
FACT_LANES: Mapping[str, Lane] = {
    NodeKind.API_ENDPOINT: Lane.API,
    NodeKind.GRAPHQL_OPERATION: Lane.API,
    NodeKind.GRAPHQL_FIELD: Lane.API,
    NodeKind.RPC_METHOD: Lane.API,
    NodeKind.ORM_MODEL: Lane.DB,
    NodeKind.TABLE: Lane.DB,
    NodeKind.COLUMN: Lane.DB,
    NodeKind.SQL_STATEMENT: Lane.DB,
}

Direction = Literal["out", "in", "both"]


@dataclass(frozen=True, slots=True)
class Node:
    symbol_key: str
    kind: str
    name: str
    qualified_name: str
    path: str | None
    start_line: int
    end_line: int
    lang: str
    lane: str = Lane.OTHER.value  # the swimlane it is drawn in (:func:`lane`)


def lane(kind: str, path: str | None, role: str | None) -> str:
    """A node's swimlane: a fact's by its kind, a definition's by its file (role and path, as
    search hits'), else ``other`` (externals, facts of no file)."""
    if kind in FACT_LANES:
        return FACT_LANES[kind].value
    if path is None:
        return Lane.OTHER.value
    return lane_of(layer_of(role, kind, path)).value


@dataclass(frozen=True, slots=True)
class Edge:
    type: EdgeType
    confidence: float
    provenance: Provenance
    evidence_path: str | None
    evidence_line: int | None


@dataclass(frozen=True, slots=True)
class Hop:
    node: Node
    edge: Edge
    direction: Literal["out", "in"]  # out: the edge goes from ``via`` to ``node``
    depth: int
    via: str  # the key of the node it was reached from


class GraphSnapshot:
    """One snapshot's graph (see the module). Nodes are indexed in the graph by position; edges
    carry their position in :attr:`edges` as payload."""

    def __init__(
        self, nodes: list[Node], edges: list[tuple[int, int, Edge]], *, version: str = ""
    ) -> None:
        self.version = version  # the graph stage run that wrote it (its details' "version")
        self.nodes = nodes
        self.edges = [edge for _, _, edge in edges]
        self._index = {node.symbol_key: i for i, node in enumerate(nodes)}
        self.graph: rx.PyDiGraph[int, int] = rx.PyDiGraph(multigraph=True)
        self.graph.add_nodes_from(range(len(nodes)))
        self.graph.add_edges_from([(a, b, i) for i, (a, b, _) in enumerate(edges)])
        self._code: tuple[rx.PyDiGraph[int, float], list[int]] | None = None

    def __len__(self) -> int:
        return len(self.nodes)

    def node(self, key: str) -> Node | None:
        index = self._index.get(key)
        return self.nodes[index] if index is not None else None

    # ------------------------------------------------------------------ building

    @classmethod
    async def build(
        cls, db: AsyncSession, snapshot_id: uuid.UUID, *, version: str = ""
    ) -> GraphSnapshot:
        """The snapshot's graph, from the database (its org set on ``db``); put together off the
        event loop."""
        rows = await db.execute(
            select(
                CodeNode.id,
                CodeNode.symbol_key,
                CodeNode.kind,
                CodeNode.name,
                CodeNode.qualified_name,
                RepoFile.path,
                CodeNode.start_line,
                CodeNode.end_line,
                CodeNode.lang,
                RepoFile.role,
            )
            .outerjoin(RepoFile, RepoFile.id == CodeNode.file_id)
            .where(CodeNode.snapshot_id == snapshot_id)
            .order_by(CodeNode.symbol_key)
        )
        node_rows = rows.all()
        evidence = RepoFile.__table__.alias("evidence")
        found = await db.execute(
            select(
                CodeEdge.src_id,
                CodeEdge.dst_id,
                CodeEdge.type,
                CodeEdge.confidence,
                CodeEdge.provenance,
                evidence.c.path,
                CodeEdge.evidence_line,
            )
            .outerjoin(evidence, evidence.c.id == CodeEdge.evidence_file_id)
            .where(CodeEdge.snapshot_id == snapshot_id)
        )
        edge_rows = found.all()
        return await asyncio.to_thread(cls._from_rows, node_rows, edge_rows, version)

    @classmethod
    def _from_rows(
        cls, node_rows: Iterable[Any], edge_rows: Iterable[Any], version: str
    ) -> GraphSnapshot:
        same = _Strings()
        nodes: list[Node] = []
        index: dict[uuid.UUID, int] = {}
        for node_id, key, kind, name, qualified, path, start, end, lang, role in node_rows:
            index[node_id] = len(nodes)
            nodes.append(
                Node(
                    key,
                    same(kind),
                    name,
                    qualified or name,
                    same(path),
                    start,
                    end,
                    same(lang or ""),
                    same(lane(kind, path, role)),
                )
            )
        edges = [
            # As the artifact stores it: a REAL's 0.9 reads back as 0.8999999761581421, and
            # ``min_conf=0.9`` mustn't depend on where the graph was read from.
            (index[src], index[dst], Edge(kind, round(float(conf), 4), prov, same(path), line))
            for src, dst, kind, conf, prov, path, line in edge_rows
            if src in index and dst in index
        ]
        return cls(nodes, edges, version=version)

    def dumps(self) -> bytes:
        """The graph as the ``graph.bin`` artifact: a header line, then the nodes and the edges
        in lines of at most :data:`CHUNK_ROWS` rows (parsed one at a time: one document of a
        large graph takes several times its size in memory to parse)."""
        nodes = [
            [n.symbol_key, n.kind, n.name, n.qualified_name, n.path, n.start_line, n.end_line,
             n.lang, n.lane]
            for n in self.nodes
        ]  # fmt: skip
        edges = [
            [a, b, e.type.value, round(e.confidence, 4), e.provenance.value, e.evidence_path,
             e.evidence_line]
            for (a, b, i) in self.graph.weighted_edge_list()
            for e in (self.edges[i],)
        ]  # fmt: skip
        header = {"format": FORMAT, "version": self.version, "nodes": len(nodes)}
        packer = zlib.compressobj(6)
        parts = [packer.compress(orjson.dumps(header) + b"\n")]
        for rows in (nodes, edges):
            for start in range(0, len(rows), CHUNK_ROWS):
                line = orjson.dumps(rows[start : start + CHUNK_ROWS]) + b"\n"
                parts.append(packer.compress(line))
        parts.append(packer.flush())
        return b"".join(parts)

    @classmethod
    def loads(cls, blob: bytes, *, max_inflated: int = MAX_INFLATED_BYTES) -> GraphSnapshot:
        lines = _lines(blob, limit=max_inflated)
        header: dict[str, Any] = {}
        with contextlib.suppress(StopIteration):
            parsed: object = orjson.loads(next(lines))
            if isinstance(parsed, dict):
                header = cast("dict[str, Any]", parsed)
        if header.get("format") != FORMAT:
            msg = f"unknown graph format: {header.get('format')!r}"
            raise ValueError(msg)
        wanted = int(header["nodes"])
        same = _Strings()  # one string for each path, kind, language and lane
        nodes: list[Node] = []
        edges: list[tuple[int, int, Edge]] = []
        for line in lines:
            rows: list[list[Any]] = orjson.loads(line)
            if len(nodes) < wanted:
                nodes += [
                    Node(key, same(kind), name, qualified, same(path), start, end, same(lang),
                         same(lane))
                    for key, kind, name, qualified, path, start, end, lang, lane in rows
                ]  # fmt: skip
            else:
                edges += [
                    (a, b, Edge(EdgeType(kind), conf, Provenance(prov), same(path), at))
                    for a, b, kind, conf, prov, path, at in rows
                ]
        if len(nodes) != wanted:
            msg = f"a truncated graph: {len(nodes)} of {wanted} nodes"
            raise ValueError(msg)
        return cls(nodes, edges, version=str(header.get("version") or ""))

    def memory_bytes(self) -> int:
        """About what the graph holds in memory, the ranking's code graph included (reserved
        until it is built: it is, at the first search over the graph)."""
        if self._code is not None:
            code_nodes, code_edges = len(self._code[1]), self._code[0].num_edges()
        else:
            code_nodes = sum(1 for n in self.nodes if n.kind in SYMBOL_KINDS)
            code_edges = 2 * sum(1 for e in self.edges if e.type in CODE_EDGES)
        return (
            len(self.nodes) * NODE_BYTES
            + len(self.edges) * EDGE_BYTES
            + code_nodes * CODE_NODE_BYTES
            + code_edges * CODE_EDGE_BYTES
        )

    # ------------------------------------------------------------------ traversals

    def _allowed(
        self, edge_index: int, types: Collection[EdgeType] | None, min_conf: float
    ) -> bool:
        edge = self.edges[edge_index]
        return edge.confidence >= min_conf and (types is None or edge.type in types)

    def neighbors(
        self,
        key: str,
        *,
        direction: Direction = "out",
        edge_types: Collection[EdgeType] | None = None,
        depth: int = 1,
        min_conf: float = 0.0,
        limit: int = 50,
    ) -> tuple[list[Hop], bool]:
        """The nodes ``depth`` hops (at most 3) from ``key``, nearest first, each once (by its
        surest edge at that depth); and whether ``limit`` cut the list short. Unknown key:
        nothing."""
        start = self._index.get(key)
        if start is None:
            return [], False
        depth = max(1, min(depth, MAX_DEPTH))
        limit = max(1, min(limit, MAX_LIMIT))
        seen = {start}
        hops: list[Hop] = []
        frontier = [start]
        for level in range(1, depth + 1):
            found: dict[int, tuple[float, Hop]] = {}
            for here in frontier:
                for there, edge_index, way in self._steps(here, direction):
                    if there in seen or not self._allowed(edge_index, edge_types, min_conf):
                        continue
                    edge = self.edges[edge_index]
                    held = found.get(there)
                    if held is None or edge.confidence > held[0]:
                        hop = Hop(self.nodes[there], edge, way, level, self.nodes[here].symbol_key)
                        found[there] = (edge.confidence, hop)
            ordered = sorted(
                found.items(), key=lambda item: (-item[1][0], item[1][1].node.symbol_key)
            )
            for there, (_, hop) in ordered:
                if len(hops) >= limit:
                    return hops, True
                seen.add(there)
                hops.append(hop)
            frontier = [there for there, _ in ordered]
            if not frontier:
                break
        return hops, False

    def _steps(
        self, node: int, direction: Direction
    ) -> Iterable[tuple[int, int, Literal["out", "in"]]]:
        if direction in ("out", "both"):
            for _, there, edge_index in self.graph.out_edges(node):
                yield there, edge_index, "out"
        if direction in ("in", "both"):
            for there, _, edge_index in self.graph.in_edges(node):
                yield there, edge_index, "in"

    def shortest_path(
        self,
        source: str,
        target: str,
        *,
        edge_types: Collection[EdgeType] | None = None,
        min_conf: float = 0.0,
    ) -> list[Hop] | None:
        """The surest path from ``source`` to ``target`` along the edges (each edge costs
        ``-log(confidence)``); None when there is none. Only the edges it may take are walked:
        a narrow filter, or a target out of reach, stops at what those edges reach."""
        a, b = self._index.get(source), self._index.get(target)
        if a is None or b is None or a == b:
            return None
        costs = {a: 0.0}
        back: dict[int, tuple[int, int]] = {}  # node: (the node before it, the edge between)
        settled: set[int] = set()
        queue = [(0.0, a)]
        while queue:
            cost, here = heapq.heappop(queue)
            if here == b:
                break
            if here in settled:
                continue
            settled.add(here)
            for _, there, edge_index in self.graph.out_edges(here):
                if there in settled or not self._allowed(edge_index, edge_types, min_conf):
                    continue
                step = cost - math.log(max(self.edges[edge_index].confidence, 1e-6)) + 1e-9
                if step < costs.get(there, math.inf):
                    costs[there] = step
                    back[there] = (here, edge_index)
                    heapq.heappush(queue, (step, there))
        else:
            return None
        steps: list[tuple[int, int, int]] = []
        there = b
        while there != a:
            here, edge_index = back[there]
            steps.append((here, there, edge_index))
            there = here
        return [
            Hop(self.nodes[there], self.edges[i], "out", level, self.nodes[here].symbol_key)
            for level, (here, there, i) in enumerate(reversed(steps), start=1)
        ]

    def reverse_closure(
        self,
        key: str,
        *,
        edge_types: Collection[EdgeType] | None = None,
        min_conf: float = 0.0,
        max_nodes: int = 1000,
    ) -> list[str]:
        """The keys of everything that reaches ``key`` (its dependents), nearest first, at most
        ``max_nodes``."""
        start = self._index.get(key)
        if start is None:
            return []
        seen = {start}
        order: list[str] = []
        queue = deque([start])
        while queue and len(order) < max_nodes:
            here = queue.popleft()
            for there, _, edge_index in self.graph.in_edges(here):
                if there in seen or not self._allowed(edge_index, edge_types, min_conf):
                    continue
                seen.add(there)
                order.append(self.nodes[there].symbol_key)
                queue.append(there)
                if len(order) >= max_nodes:
                    break
        return order

    # ------------------------------------------------------------------ ranking

    def _code_graph(self) -> tuple[rx.PyDiGraph[int, float], list[int]]:
        """The definitions and the code edges between them, each edge both ways (weighted by
        confidence, the reverse at half), built once."""
        if self._code is None:
            kept = [i for i, n in enumerate(self.nodes) if n.kind in SYMBOL_KINDS]
            position = {node: i for i, node in enumerate(kept)}
            code: rx.PyDiGraph[int, float] = rx.PyDiGraph(multigraph=False)
            code.add_nodes_from(kept)
            weights: dict[tuple[int, int], float] = {}
            for a, b, edge_index in self.graph.weighted_edge_list():
                edge = self.edges[edge_index]
                if edge.type not in CODE_EDGES or a not in position or b not in position or a == b:
                    continue
                pa, pb = position[a], position[b]
                weights[pa, pb] = max(weights.get((pa, pb), 0.0), edge.confidence)
                weights[pb, pa] = max(weights.get((pb, pa), 0.0), edge.confidence / 2)
            code.add_edges_from([(a, b, w) for (a, b), w in weights.items()])
            self._code = (code, kept)
        return self._code

    def personalized_pagerank(
        self,
        seeds: Mapping[str, float],
        *,
        alpha: float = 0.85,
        limit: int = 50,
        hops: int = 3,
        max_nodes: int = PPR_MAX_NODES,
    ) -> list[tuple[str, float]]:
        """Definitions ranked by personalized PageRank from ``seeds`` (key to weight; keys that
        aren't definitions are ignored), the seeds left out; best first. Ranked over the code
        within ``hops`` of a seed (at most ``max_nodes`` definitions, nearest kept): what is
        further gets next to no rank, and a query never walks the whole repository."""
        code, kept = self._code_graph()
        position = {node: i for i, node in enumerate(kept)}
        starts = {
            position[self._index[key]]: weight
            for key, weight in seeds.items()
            if weight > 0 and key in self._index and self._index[key] in position
        }
        if not starts:
            return []
        near = _around(code, starts, hops, max_nodes)
        local = code.subgraph(near)
        if local.num_edges() == 0:
            return []
        # The subgraph numbers its nodes its own way; each still holds its index in the graph.
        seeded = {kept[node]: weight for node, weight in starts.items()}
        ranks = rx.pagerank(
            local,
            alpha=alpha,
            personalization={
                i: seeded[local[i]] for i in local.node_indices() if local[i] in seeded
            },
            weight_fn=float,
            max_iter=100,
            tol=1e-6,
        )
        ranked = sorted(
            ((local[i], score) for i, score in ranks.items() if local[i] not in seeded),
            key=lambda item: (-item[1], self.nodes[item[0]].symbol_key),
        )
        return [(self.nodes[i].symbol_key, score) for i, score in ranked[:limit] if score > 0]


def _around(
    graph: rx.PyDiGraph[int, float], starts: Iterable[int], hops: int, max_nodes: int
) -> list[int]:
    """The nodes within ``hops`` of ``starts`` (breadth first, at most ``max_nodes``)."""
    seen = dict.fromkeys(starts)
    frontier = list(seen)
    for _ in range(hops):
        following: list[int] = []
        for node in frontier:
            for there in graph.successor_indices(node):
                if there not in seen:
                    if len(seen) >= max_nodes:
                        return list(seen)
                    seen[there] = None
                    following.append(there)
        frontier = following
    return list(seen)


NEIGHBOURHOOD_NAMES = 8  # callees, then callers, named in a neighbourhood
NEIGHBOURHOOD_MIN_CONF = 0.4  # guesses among several candidates aren't named


def neighbourhood(
    graph: GraphSnapshot, key: str, *, names: int = NEIGHBOURHOOD_NAMES
) -> str | None:
    """What a definition calls and what calls it, one line each, for the listwise reranker
    (3.6.5): each neighbour's name, surest first; one in another lane than the definition's is
    tagged with it (``save [db]``): the edges where the code crosses a boundary. None when the
    graph has neither."""
    node = graph.node(key)
    if node is None:
        return None
    lines: list[str] = []
    ways: tuple[tuple[str, Direction], ...] = (("calls", "out"), ("called by", "in"))
    for label, direction in ways:
        hops, truncated = graph.neighbors(
            key,
            direction=direction,
            edge_types=CALL_EDGES,
            min_conf=NEIGHBOURHOOD_MIN_CONF,
            limit=names,
        )
        if not hops:
            continue
        named = [
            hop.node.name if hop.node.lane == node.lane else f"{hop.node.name} [{hop.node.lane}]"
            for hop in hops
        ]
        lines.append(f"{label}: {', '.join(named)}{', ...' if truncated else ''}")
    return "\n".join(lines) or None


def _lines(blob: bytes, *, step: int = 1 << 20, limit: int = MAX_INFLATED_BYTES) -> Iterator[bytes]:
    """The artifact's lines, decompressed ``step`` bytes at a time; ValueError if it is cut
    short, has bytes after its end, or inflates past ``limit``."""
    unpacker = zlib.decompressobj()
    inflated = 0
    rest = b""

    def take(out: bytes) -> list[bytes]:
        nonlocal inflated, rest
        inflated += len(out)
        if inflated > limit:
            msg = f"a graph artifact inflating past {limit:,} bytes"
            raise ValueError(msg)
        *whole, rest = (rest + out).split(b"\n")
        return whole

    for start in range(0, len(blob), step):
        pending = blob[start : start + step]
        while pending:
            if unpacker.eof:
                msg = "a graph artifact with bytes after its end"
                raise ValueError(msg)
            yield from take(unpacker.decompress(pending, step))
            pending = unpacker.unconsumed_tail
    yield from take(unpacker.flush())
    if not unpacker.eof:
        msg = "a truncated graph artifact"
        raise ValueError(msg)
    if unpacker.unused_data:
        msg = "a graph artifact with bytes after its end"
        raise ValueError(msg)
    if rest:
        yield rest


class _Strings:
    """One string object for each value seen (paths, kinds, languages, lanes repeat by the
    thousand: kept once, a graph takes far less memory)."""

    def __init__(self) -> None:
        self._seen: dict[Any, Any] = {}

    def __call__(self, value: Any) -> Any:  # a str, or None
        if value is None:
            return None
        return self._seen.setdefault(value, value)
