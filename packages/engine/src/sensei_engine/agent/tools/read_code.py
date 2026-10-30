"""``read_code``: a file's lines at the commit, citable (at most :data:`MAX_LINES` a call)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sensei_engine.agent.tools.base import (
    Results,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    code_result,
    file_lines,
    file_path,
    lines_within,
)

MAX_LINES = 300
EXAMPLE = 'read_code({"path": "backend/app/crud.py", "start_line": 1, "end_line": 80})'

DESCRIPTION = (
    "Read lines of a file of the repository at this commit, as a citable search result (each "
    "line after its number). Use it to read the code a search, a symbol or a trace hop points "
    "at before you describe or cite it, or to read on past where a result stopped. Give the "
    f"lines you need: at most {MAX_LINES} a call (start_line 1, end_line {MAX_LINES} by "
    "default); a very long one may be cut shorter, and then says where to read on from. Don't "
    "use it to find code (use code_search, grep or find_symbol) or to list a directory (use "
    "list_dir)."
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The file, from the repository's root."},
        "start_line": {"type": "integer", "description": "The first line (1-based)."},
        "end_line": {"type": "integer", "description": "The last line (inclusive)."},
    },
    "required": ["path"],
    "additionalProperties": False,
}

EXAMPLES = (
    {"path": "backend/app/crud.py", "start_line": 1, "end_line": 80},
    {"path": "frontend/src/routes/login.tsx"},
)


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    path = file_path(ctx, str(args.get("path", "")), example=EXAMPLE)
    lines = file_lines(ctx, path, example=EXAMPLE)
    start = int(args.get("start_line") or 1)
    end = int(args.get("end_line") or start + MAX_LINES - 1)
    if start < 1 or end < start:
        raise ToolError(
            f"start_line must be 1 or more and end_line at least start_line (got {start}-{end}).",
            example=EXAMPLE,
        )
    if not lines:
        return Results().done(f"{path} is empty", hits=0, narrow="")
    if start > len(lines):
        raise ToolError(
            f"{path} has {len(lines)} lines; start_line {start} is past its end.",
            example=f'read_code({{"path": "{path}", "start_line": 1, "end_line": {len(lines)}}})',
        )
    end = min(end, len(lines), start + MAX_LINES - 1)
    out = Results()
    wanted = lines[start - 1 : end]
    shown = wanted[: max(1, lines_within(wanted, out.left - 50))]
    last = start + len(shown) - 1
    out.code(*code_result(ctx, path, start, shown, title=f"{path} ({len(lines)} lines)"))
    if last < end:
        out.text(f"Cut at line {last} to keep this answer short; read on from {last + 1}.")
    elif end < len(lines):
        out.text(f"{path} goes on to line {len(lines)}.")
    return out.done(f"{path}:{start}-{last}", hits=1, narrow="ask for fewer lines")


TOOL = Tool("read_code", DESCRIPTION, SCHEMA, EXAMPLES, run)
