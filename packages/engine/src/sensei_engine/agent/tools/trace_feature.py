"""``trace_feature``: a feature's flow across the layers (3.13, 3.16, 3.24), from the page or
the endpoint it starts at to the database and back, as the trace page shows it."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from sensei_engine.agent.tools.base import (
    Results,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    readable_id,
    require_graph,
    symbol_key_of,
    trail_of,
)
from sensei_engine.graph.trace import TraceError, TracePath, trace_feature

DESCRIPTION = (
    "Trace a feature's flow across the layers of the repository: from a page or component, "
    "through its hooks and the HTTP request it sends, to the endpoint, its handler, the "
    "services it calls and the tables it reads and writes, and what comes back to the page. "
    'Use it for any question about how something works end to end ("what happens when a user '
    'logs in", "how is an item created"). The entry is a symbol id or node key, an endpoint '
    '("POST /api/v1/items/"), a file path, a page or function name, or the feature in words '
    '("create item"); feature says which of an entry\'s features to follow when a page has '
    'several ("delete" on a page that lists, adds and deletes). direction down (default: '
    "from the entry onward), up (where the entry is reached from) or both. Each path lists "
    "its hops with their layer, file and lines, and how each was reached; a gap says where "
    "the graph has no link, and why. The hops are graph facts: read the code with read_code "
    "before citing it."
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "entry": {"type": "string", "description": "Where to start (see the description)."},
        "feature": {"type": "string", "description": 'Which feature, in words: "add item".'},
        "direction": {
            "type": "string",
            "enum": ["down", "up", "both"],
            "description": "down (default), up or both.",
        },
        "min_conf": {"type": "number", "description": "Only links at least this sure, 0 to 1."},
    },
    "required": ["entry"],
    "additionalProperties": False,
}

EXAMPLES = (
    {"entry": "sym:frontend/src/routes/login.tsx::Login"},
    {"entry": "POST /api/v1/items/", "direction": "both"},
    {"entry": "ItemsPage", "feature": "delete item"},
)


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    graph = require_graph(ctx)
    entry = str(args.get("entry", "")).strip()
    if not entry:
        raise ToolError(
            "entry is empty.", example='trace_feature({"entry": "POST /api/v1/items/"})'
        )
    if entry.startswith("sym:"):
        entry = await symbol_key_of(ctx, entry)
    direction = args.get("direction") or "down"
    if direction not in ("down", "up", "both"):
        raise ToolError(
            "direction must be down, up or both.",
            example='trace_feature({"entry": "POST /api/v1/items/", "direction": "up"})',
        )
    min_conf = max(0.0, min(float(args.get("min_conf") or 0.0), 1.0))
    feature = str(args.get("feature") or "").strip() or None
    try:
        traced = await asyncio.to_thread(
            trace_feature, graph, entry, min_conf=min_conf, direction=direction, feature=feature
        )
    except TraceError as exc:
        raise ToolError(
            f"{exc} Try a page or function name, an endpoint, or the feature in words.",
            example='trace_feature({"entry": "create item"})',
        ) from exc
    out = Results()
    starts = ", ".join(
        readable_id(n.symbol_key, n.kind, n.path, n.qualified_name) for n in traced.entries
    )
    out.text(f"From {starts}." if starts else "No entry.")
    for n, path in enumerate(traced.paths, start=1):
        out.text(describe_path(n, path))
    if not traced.paths:
        out.text("No path leads on from the entry: it calls nothing the graph links.")
    narrow = "trace from a later hop, or with a feature"
    result = out.done(f"{len(traced.paths)} paths", hits=len(traced.paths), narrow=narrow)
    result.trail = trail_of(traced.paths[0]) if traced.paths else None
    return result


def describe_path(n: int, path: TracePath) -> str:
    """A path as the agent reads it: its layers, then each hop (layer, id, place, how it was
    reached), then where it stops short."""
    layers = " → ".join(layer for layer in path.layers)
    reached = "reaches the data" if path.reaches_goal else "stops before the data"
    lines = [f"Path {n} ({layers}; {reached}; confidence {path.confidence:.2f}):"]
    for i, hop in enumerate(path.hops):
        node = hop.node
        name = readable_id(node.symbol_key, node.kind, node.path, node.qualified_name)
        place = f"  {node.path}:{node.start_line}-{node.end_line}" if node.path else ""
        how = ""
        if hop.edge is not None:
            kind = (hop.edge_type or hop.edge.type).value
            how = f"  [{kind} {hop.edge.confidence:.2f}]"
            if hop.alternatives > 1:
                how += f" (one of {hop.alternatives} targets)"
        marks: list[str] = []
        if i == path.entry_index and path.entry_index:
            marks.append("entry")
        if path.return_index is not None and i == path.return_index:
            marks.append("returns")
        mark = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"  {i + 1}. {hop.layer:<8} {name}{mark}{place}{how}")
    for gap in path.breaks:
        lines.append(f"  {gap.kind} after hop {gap.after_hop + 1}: {gap.reason}")
    return "\n".join(lines)


TOOL = Tool("trace_feature", DESCRIPTION, SCHEMA, EXAMPLES, run)
