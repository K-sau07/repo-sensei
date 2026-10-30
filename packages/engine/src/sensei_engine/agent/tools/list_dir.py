"""``list_dir``: a directory of the snapshot, as the code browser's tree lists it (only what
the index kept: no ignored or vendored files)."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from typing import Any

from sensei_engine.agent.tools.base import (
    Results,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    args_key,
    file_path,
    offset_of,
    page_note,
)

PAGE = 200  # entries per page
EXAMPLE = 'list_dir({"path": "backend/app"})'

DESCRIPTION = (
    "List a directory of the repository at this commit: its subdirectories (with how many "
    "files each holds) and files. Use it to get your bearings in an unfamiliar repository or "
    "package, to find where a layer lives (routes, models, components), or to see a module's "
    'files before reading one. The root is "" (default). depth 2 also lists each '
    "subdirectory's entries. Ignored and vendored files aren't listed. Don't use it to search "
    "(use code_search, grep or find_symbol)."
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": 'The directory ("" for the root).'},
        "depth": {"type": "integer", "description": "1 (default) or 2 levels."},
        "cursor": {"type": "string", "description": "A previous result's next_cursor."},
    },
    "required": [],
    "additionalProperties": False,
}

EXAMPLES = ({"path": ""}, {"path": "backend/app", "depth": 2})


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    raw = str(args.get("path") or "")
    path = file_path(ctx, raw, example=EXAMPLE) if raw.strip("/ .") else ""
    depth = max(1, min(int(args.get("depth") or 1), 2))
    key = args_key("list_dir", args)
    offset = offset_of(args.get("cursor"), key)
    files = _under(ctx.tree, path)
    if not files:
        raise ToolError(
            f"There is no directory {path or '(root)'} at this commit.", example=EXAMPLE
        )
    entries = _entries(files, path, depth)
    page = entries[offset : offset + PAGE]
    out = Results()
    out.text(f"{path or '(root)'}/  ({len(files)} files)\n" + "\n".join(page))
    narrow = "list a subdirectory instead"
    out.text(page_note(offset, len(page), len(entries), key, exact=True, narrow=narrow))
    return out.done(
        f"{len(entries)} entries in {path or 'the root'}", hits=len(entries), narrow=narrow
    )


def _under(tree: Sequence[str], path: str) -> Sequence[str]:
    """The files under ``path`` (the whole tree for the root), from the sorted tree."""
    if not path:
        return tree
    prefix = path + "/"
    start = bisect_left(tree, prefix)
    end = bisect_left(tree, path + "0")  # "0" is the character after "/"
    return tree[start:end]


def _entries(files: Sequence[str], base: str, depth: int) -> list[str]:
    """The entries of directory ``base`` (``files``, sorted, are those under it): directories
    first, each ``name/  (n files)``, then files; ``depth`` 2 lists each directory's under it,
    indented."""
    cut = len(base) + 1 if base else 0
    counts: dict[str, int] = {}
    here: list[str] = []
    for path in files:
        head, sep, _ = path[cut:].partition("/")
        if sep:
            counts[head] = counts.get(head, 0) + 1
        else:
            here.append(head)
    lines: list[str] = []
    for name in sorted(counts):
        lines.append(f"{name}/  ({counts[name]} files)")
        if depth > 1:
            inner = f"{base}/{name}" if base else name
            lines += [f"  {line}" for line in _entries(_under(files, inner), inner, depth - 1)]
    return lines + here


TOOL = Tool("list_dir", DESCRIPTION, SCHEMA, EXAMPLES, run)
