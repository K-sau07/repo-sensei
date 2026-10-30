"""What a tool found, as a card the Ask page draws beside the answer (4.11.3): the definitions
``find_symbol`` found, the neighbours ``graph_neighbors`` walked (as the graph API sends them, so
the page's mini graph draws them as it is), and the paths ``trace_feature`` traced.

A card is plain JSON, kept with the tool's part of the message; it holds names, paths and lines
the tool read off the index, nothing the model wrote. Each is capped so a message stays small.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sensei_engine.graph.snapshot import Edge, Hop, Node
from sensei_engine.graph.trace import TracePath
from sensei_engine.symbols.lookup import SymbolHit

MAX_SYMBOLS = 10
MAX_NEIGHBOURS = 24
MAX_PATHS = 3
MAX_HOPS = 16

Card = dict[str, Any]


def graph_node(node: Node) -> dict[str, Any]:
    """A node as the graph API sends it (``GraphNodeOut``)."""
    return {
        "symbol_key": node.symbol_key,
        "kind": node.kind,
        "name": node.name,
        "qualified_name": node.qualified_name,
        "path": node.path,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "lang": node.lang,
        "lane": node.lane,
    }


def _edge(edge: Edge) -> dict[str, Any]:
    return {
        "type": edge.type.value,
        "confidence": round(edge.confidence, 4),
        "provenance": edge.provenance.value,
        "evidence": {"path": edge.evidence_path, "line": edge.evidence_line}
        if edge.evidence_path is not None
        else None,
    }


def graph_hop(hop: Hop) -> dict[str, Any]:
    """A hop as the graph API sends it (``GraphHopOut``)."""
    return {
        "node": graph_node(hop.node),
        "edge": {**_edge(hop.edge), "direction": hop.direction},
        "depth": hop.depth,
        "via": hop.via,
    }


def symbols_card(hits: Sequence[SymbolHit]) -> Card:
    return {
        "kind": "symbols",
        "symbols": [
            {
                "symbolKey": hit.symbol_key,
                "name": hit.name,
                "qualifiedName": hit.qualified_name,
                "kind": hit.kind,
                "path": hit.path,
                "lines": [hit.start_line, hit.end_line],
                "signature": hit.signature,
            }
            for hit in hits[:MAX_SYMBOLS]
        ],
        "more": max(0, len(hits) - MAX_SYMBOLS),
    }


def graph_card(root: Node, hops: Sequence[Hop]) -> Card:
    return {
        "kind": "graph",
        "root": graph_node(root),
        "hops": [graph_hop(hop) for hop in hops[:MAX_NEIGHBOURS]],
        "more": max(0, len(hops) - MAX_NEIGHBOURS),
    }


def trace_card(entry: str, paths: Sequence[TracePath]) -> Card:
    """The surest paths (the trace's order), each its hops through the layers and where it
    stops short."""
    return {
        "kind": "trace",
        "entry": entry,
        "paths": [
            {
                "confidence": round(path.confidence, 4),
                "reachesGoal": path.reaches_goal,
                "layers": list(path.layers),
                "hops": [
                    {
                        "layer": hop.layer,
                        "node": graph_node(hop.node),
                        "edge": {
                            **_edge(hop.edge),
                            "type": (hop.edge_type or hop.edge.type).value,
                        }
                        if hop.edge is not None
                        else None,
                    }
                    for hop in path.hops[:MAX_HOPS]
                ],
                "cut": max(0, len(path.hops) - MAX_HOPS),
                "breaks": [
                    {"afterHop": b.after_hop, "reason": b.reason, "kind": b.kind}
                    for b in path.breaks
                    if b.after_hop < MAX_HOPS
                ],
            }
            for path in paths[:MAX_PATHS]
        ],
        "more": max(0, len(paths) - MAX_PATHS),
    }
