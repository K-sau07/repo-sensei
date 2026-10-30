"""``grep``: an exact string or a regular expression over the snapshot's files (ripgrep, as the
code browser's search runs it): each file's matching lines, with a little context, as one
citable result."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sensei_engine.agent.tools.base import (
    Results,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    args_key,
    offset_of,
    page_note,
    runs_result,
)
from sensei_engine.search.grep import (
    MAX_LIMIT as GREP_MAX,
)
from sensei_engine.search.grep import (
    Case,
    GrepBusyError,
    GrepHit,
    GrepQuery,
    GrepUnavailableError,
    InvalidQueryError,
)

DEFAULT_LIMIT = 50  # matching lines per page
MAX_LIMIT = 200
CONTEXT = 2  # lines shown before and after each match

DESCRIPTION = (
    "Find an exact string, or a regular expression, in every file of the repository at this "
    "commit (ripgrep). Use it for a literal you know: an error message, a route path "
    '("/api/v1/items"), a config key, an identifier\'s every use, a TODO. Each file with '
    "matches is one citable search result: its matching lines, two lines of context around "
    "each, files with more matches first. Don't use it to search by meaning (use "
    "code_search) or to find where a name is defined (use find_symbol). The pattern is a "
    "fixed string unless regex is true; matching is case-insensitive while the pattern is all "
    'lower case. Narrow with path_glob ("frontend/src/**/*.tsx"); page with next_cursor.'
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "The text (or regular expression)."},
        "regex": {"type": "boolean", "description": "The pattern is a regular expression."},
        "path_glob": {"type": "string", "description": 'Only these paths: "backend/**/*.py".'},
        "limit": {"type": "integer", "description": f"Matching lines per page, 1 to {MAX_LIMIT}."},
        "cursor": {"type": "string", "description": "A previous result's next_cursor."},
    },
    "required": ["pattern"],
    "additionalProperties": False,
}

EXAMPLES = (
    {"pattern": "/api/v1/items"},
    {"pattern": "def (create|update)_item", "regex": True, "path_glob": "backend/**"},
)


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    pattern = str(args.get("pattern", ""))
    if not pattern.strip():
        raise ToolError("pattern is empty.", example='grep({"pattern": "/api/v1/items"})')
    limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    key = args_key("grep", args)
    offset = offset_of(args.get("cursor"), key)
    if offset + limit >= GREP_MAX:
        raise ToolError(
            f"grep reads at most the first {GREP_MAX} matching lines; narrow the search instead "
            "of paging further.",
            example=f'grep({{"pattern": "{pattern[:40]}", "path_glob": "backend/**"}})',
        )
    glob = args.get("path_glob") or None
    try:
        query = GrepQuery(
            pattern,
            regex=bool(args.get("regex")),
            case=Case.SMART,
            globs=(glob,) if glob else (),
            limit=offset + limit + 1,  # one more: whether a next page has any
            context=CONTEXT,
        )
        found = await ctx.exact.search(ctx.root, query, files=ctx.searchable)
    except InvalidQueryError as exc:
        raise ToolError(
            f"That pattern can't be searched: {exc}. A regular expression is Rust's syntax; "
            "for literal text leave regex false.",
            example='grep({"pattern": "items/{id}", "regex": false})',
        ) from exc
    except GrepBusyError as exc:
        raise ToolError("Too many searches are running; try the same call again.") from exc
    except GrepUnavailableError as exc:
        raise ToolError("Exact search isn't available right now; use code_search.") from exc
    hits = found.hits[offset : offset + limit]
    more = len(found.hits) > offset + limit or found.truncated
    out = Results()
    files = _by_file(hits)
    for path, matched in files.items():
        out.code(*runs_result(ctx, path, _runs(matched), title=path))
    narrow = "narrow with path_glob or a more precise pattern"
    total = offset + len(hits) + (1 if more else 0)
    note = page_note(offset, len(hits), total, key, exact=not more, narrow=narrow)
    if found.timed_out:
        note += " (The search hit its time limit: there may be more matches than shown.)"
    out.text(note)
    return out.done(f"{len(hits)} matches in {len(files)} files", hits=len(hits), narrow=narrow)


def _by_file(hits: Sequence[GrepHit]) -> dict[str, list[GrepHit]]:
    files: dict[str, list[GrepHit]] = {}
    for hit in hits:
        files.setdefault(hit.path, []).append(hit)
    return files


def _runs(hits: Sequence[GrepHit]) -> list[tuple[int, tuple[str, ...]]]:
    """A file's matches with their context, as runs of consecutive lines (overlapping context
    shown once): one result block each."""
    lines: dict[int, str] = {}
    for hit in hits:
        start = hit.line - len(hit.before)
        for n, text in enumerate([*hit.before, hit.text, *hit.after], start=start):
            lines.setdefault(n, text)
    runs: list[tuple[int, list[str]]] = []
    for n in sorted(lines):
        if runs and runs[-1][0] + len(runs[-1][1]) == n:
            runs[-1][1].append(lines[n])
        else:
            runs.append((n, [lines[n]]))
    return [(first, tuple(text)) for first, text in runs]


TOOL = Tool("grep", DESCRIPTION, SCHEMA, EXAMPLES, run)
