"""The agent's tools (4.1): read-only, cite-ready views of one indexed snapshot, the model's
hands. Each is a :class:`~sensei_engine.agent.tools.base.Tool`; :data:`TOOLS` is the order the
model is given them in (part of the cached prefix: change it and every cache misses)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sensei_engine.agent.tools import (
    code_search,
    find_symbol,
    get_feature_map,
    graph_neighbors,
    grep,
    list_dir,
    read_code,
    submit_evidence,
    trace_feature,
)
from sensei_engine.agent.tools.base import (
    CitationIndex,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    failed,
)

TOOLS: tuple[Tool, ...] = (
    code_search.TOOL,
    grep.TOOL,
    find_symbol.TOOL,
    graph_neighbors.TOOL,
    trace_feature.TOOL,
    read_code.TOOL,
    list_dir.TOOL,
    get_feature_map.TOOL,
    submit_evidence.TOOL,
)
BY_NAME = {tool.name: tool for tool in TOOLS}


def definitions() -> list[dict[str, Any]]:
    """The tools as the Messages API takes them, in :data:`TOOLS` order."""
    return [tool.definition() for tool in TOOLS]


async def run_tool(ctx: ToolContext, name: str, args: Mapping[str, Any]) -> ToolResult:
    """What the tool ``name`` answers ``args`` with; a call it can't answer is an error result
    that says how to make one it can (never an exception: the run goes on)."""
    tool = BY_NAME.get(name)
    if tool is None:
        known = ", ".join(BY_NAME)
        return failed(ToolError(f"There is no tool {name!r}; the tools are {known}."))
    try:
        return await tool.run(ctx, args)
    except ToolError as error:
        return failed(error)
    except (TypeError, ValueError) as exc:  # an argument of the wrong type (int("ten"))
        return failed(ToolError(f"An argument has the wrong type: {exc}."))


__all__ = [
    "BY_NAME",
    "TOOLS",
    "CitationIndex",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "definitions",
    "run_tool",
]
