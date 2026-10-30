"""``graph_neighbors``: what a symbol calls, renders, reads, imports, is called by... (the code
graph and its cross-layer links, as the graph page walks them)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from sensei_core.models import EdgeType
from sensei_engine.agent.cards import graph_card
from sensei_engine.agent.tools.base import (
    FIND_EXAMPLE,
    Results,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    readable_id,
    require_graph,
    symbol_key_of,
)
from sensei_engine.graph.snapshot import Hop

LIMIT = 40

DESCRIPTION = (
    "List what a symbol is connected to in the repository's graph: what it calls, renders, "
    "uses as a hook, injects, imports, inherits, the endpoint a request targets, the handler "
    "an endpoint runs, the tables code reads and writes (direction out); what calls or uses "
    "it (in); or both. Each neighbour comes with the edge's type, confidence (1.0: resolved by "
    "the compiler or a contract; lower: inferred from names or URLs) and the line the edge was "
    "found on. Use it to follow a flow one step at a time, to find callers before changing "
    "something, or to see where data is read and written. depth 2 also lists the neighbours' "
    "neighbours. The symbol is an id from find_symbol or code_search (sym:<path>::<name>) or "
    'a node key ("api_endpoint POST /api/v1/items/"). For a whole flow from a page to the '
    "database, use trace_feature; to read the code, read_code."
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "A symbol id or a node key."},
        "direction": {
            "type": "string",
            "enum": ["out", "in", "both"],
            "description": "out (default): what it uses; in: what uses it.",
        },
        "edge_types": {
            "type": "array",
            "items": {"type": "string", "enum": [t.value for t in EdgeType]},
            "description": "Only edges of these types (default: every type).",
        },
        "depth": {"type": "integer", "description": "1 (default) or 2 hops."},
        "min_conf": {"type": "number", "description": "Only edges at least this sure, 0 to 1."},
    },
    "required": ["symbol"],
    "additionalProperties": False,
}

EXAMPLES = (
    {"symbol": "sym:backend/app/api/routes/items.py::create_item"},
    {
        "symbol": "sym:backend/app/models.py::Item",
        "direction": "in",
        "edge_types": ["reads", "writes"],
    },
)


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    graph = require_graph(ctx)
    symbol = str(args.get("symbol", "")).strip()
    if not symbol:
        raise ToolError("symbol is empty.", example=FIND_EXAMPLE)
    key = await symbol_key_of(ctx, symbol)
    node = graph.node(key)
    if node is None:
        raise ToolError(
            f"{symbol} is not in the graph of this commit. Find the symbol's id with "
            "find_symbol (a module, a test helper or generated code may have no edges).",
            example=FIND_EXAMPLE,
        )
    direction = args.get("direction") or "out"
    if direction not in ("out", "in", "both"):
        raise ToolError(
            "direction must be out, in or both.",
            example=f'graph_neighbors({{"symbol": "{symbol}", "direction": "in"}})',
        )
    try:
        types = {EdgeType(t) for t in args.get("edge_types") or ()} or None
    except ValueError as exc:
        raise ToolError(
            f"edge_types are some of {', '.join(t.value for t in EdgeType)}.",
            example=f'graph_neighbors({{"symbol": "{symbol}", "edge_types": ["calls"]}})',
        ) from exc
    depth = max(1, min(int(args.get("depth") or 1), 2))
    min_conf = max(0.0, min(float(args.get("min_conf") or 0.0), 1.0))
    hops, truncated = await asyncio.to_thread(
        graph.neighbors,
        key,
        direction=direction,
        edge_types=types,
        depth=depth,
        min_conf=min_conf,
        limit=LIMIT,
    )
    out = Results()
    head = readable_id(node.symbol_key, node.kind, node.path, node.qualified_name)
    found = [_hop(hop) for hop in hops] or ["  (no neighbours with these filters)"]
    taken = out.lines(
        found, head=f"{head}  ({node.kind}){_place(node.path, node.start_line, node.end_line)}"
    )
    if truncated or taken < len(found):
        out.note(f"Only the first {taken} are shown: narrow with edge_types or min_conf.")
    result = out.done(f"{len(hops)} neighbours", hits=len(hops), narrow="narrow with edge_types")
    result.card = graph_card(node, hops)
    return result


def _place(path: str | None, start: int, end: int) -> str:
    return f"  {path}:{start}-{end}" if path else ""


def _hop(hop: Hop) -> str:
    node, edge = hop.node, hop.edge
    arrow = "→" if hop.direction == "out" else "←"
    name = readable_id(node.symbol_key, node.kind, node.path, node.qualified_name)
    where = f", at {edge.evidence_path}:{edge.evidence_line}" if edge.evidence_path else ""
    indent = "  " * hop.depth
    return (
        f"{indent}{arrow} {edge.type.value} ({edge.confidence:.2f} {edge.provenance.value}"
        f"{where}) {name}  ({node.kind}){_place(node.path, node.start_line, node.end_line)}"
    )


TOOL = Tool("graph_neighbors", DESCRIPTION, SCHEMA, EXAMPLES, run)
