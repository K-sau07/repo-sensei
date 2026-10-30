"""``get_feature_map``: a module's precomputed map (its purpose, entry points, key symbols and
data flow), made after indexing (4.9): found by the module's path, or by the words of the
feature; its entry points shown as citable code. A map whose files changed since says so."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sensei_core.models import FeatureMap
from sensei_engine.agent.tools.base import (
    Results,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    code_result,
    file_lines,
)
from sensei_engine.llm import feature_maps

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
    wanted = str(args.get("feature_or_path", "")).strip()
    if not wanted:
        raise ToolError(
            "feature_or_path is empty.",
            example='get_feature_map({"feature_or_path": "item creation"})',
        )
    out = Results()
    found = await feature_maps.find(ctx.db, ctx.snapshot_id, wanted)
    if not found:
        out.text(
            f"No feature map covers {wanted!r} at this commit. Use code_search, "
            "trace_feature, list_dir and read_code instead."
        )
        return out.done("no map", hits=0, narrow="")
    for one in found:
        fresh = await feature_maps.current(ctx.db, one)
        out.text(_described(one, fresh=fresh))
        for entry in one.entry_points:
            lines = file_lines(ctx, entry["path"], example="")
            start, end = entry["start_line"], min(entry["end_line"], entry["start_line"] + 19)
            shown = lines[start - 1 : end]
            if shown:
                title = f"{entry['path']}: {entry['why']}"
                out.code(*code_result(ctx, entry["path"], start, shown, title=title))
    narrow = "ask for one module by its path"
    return out.done(f"{len(found)} maps", hits=len(found), narrow=narrow)


def _described(one: FeatureMap, *, fresh: bool) -> str:
    lines = [f"{one.title} ({one.path}/)", one.summary]
    if not fresh:
        lines.append("(Its files changed since it was written: check what it says in the code.)")
    if one.entry_points:
        lines.append("Entry points:")
        lines += [f"- {_place(e)}: {e['why']}" for e in one.entry_points]
    if one.key_symbols:
        lines.append("Key symbols:")
        lines += [f"- {_place(e)}: {e['why']}" for e in one.key_symbols]
    if one.data_flow:
        lines.append("Data flow:")
        lines += [f"{n}. {step}" for n, step in enumerate(one.data_flow, start=1)]
    return "\n".join(lines)


def _place(entry: Mapping[str, Any]) -> str:
    return f"{entry['symbol_key']} {entry['path']}:{entry['start_line']}-{entry['end_line']}"


TOOL = Tool("get_feature_map", DESCRIPTION, SCHEMA, EXAMPLES, run)
