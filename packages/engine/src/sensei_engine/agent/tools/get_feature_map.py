"""``get_feature_map``: a feature's precomputed map (its purpose, entry points, key symbols and
data flow). Feature maps are made after indexing from 4.9 on; until then the tool says so."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sensei_engine.agent.tools.base import Results, Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Get the precomputed map of a feature or a module: what it is for, its entry points, its "
    "key symbols and how data flows through it, with citations. Use it first for a broad "
    'question about a feature or a part of the repository ("how does billing work", "what '
    'is in backend/app/api"), then read the code it cites. Give the feature in words or a '
    "directory path. When no map is available it says so: answer from code_search, "
    "trace_feature, list_dir and read_code instead."
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "feature_or_path": {
            "type": "string",
            "description": 'The feature in words ("item creation") or a directory path.',
        }
    },
    "required": ["feature_or_path"],
    "additionalProperties": False,
}

EXAMPLES = ({"feature_or_path": "item creation"}, {"feature_or_path": "backend/app/api"})


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    out = Results()
    out.text(
        "Feature maps aren't available for this repository yet. Use code_search, "
        "trace_feature, list_dir and read_code instead."
    )
    return out.done("not available", hits=0, narrow="")


TOOL = Tool("get_feature_map", DESCRIPTION, SCHEMA, EXAMPLES, run)
