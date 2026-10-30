"""``code_search``: the hybrid search (2.9, reranked by 2.11) over the snapshot, as the search
page runs it: each result the definition (or the lines) it found, as a citable block."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select

from sensei_core.models import CodeNode
from sensei_engine.agent.tools.base import (
    LANES,
    Results,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    args_key,
    code_result,
    lines_within,
    offset_of,
    page_note,
    readable_id,
)
from sensei_engine.search.hybrid import WorktreeGrep
from sensei_engine.search.service import (
    ChunkText,
    Result,
    SearchMode,
    SearchQuery,
    SemanticNotReadyError,
    read_contents,
    snippet,
)

DEFAULT_LIMIT = 8
MAX_LIMIT = 20
DETAILED_LINES = 60  # its definition's, in the detailed one
GLOB_CHARS = frozenset("*?[")

DESCRIPTION = (
    "Search the repository's code by meaning and by its words at once: hybrid keyword, "
    "symbol, semantic and call-graph search, reranked. Use it first for a question in words "
    '("where are passwords hashed?", "how is an item created"), or to find the code of a '
    "feature, a layer or a concept when you don't know its names. Each result is the "
    "definition (or the lines) that matched, as a citable search result, best first. Don't "
    "use it for an exact string or a regular expression (use grep), for a name you know "
    "(use find_symbol), or to read more of a file you already found (use read_code). Narrow "
    'with path_glob ("backend/**"), lang ("python") or layer (ui, api, svc, db, other); '
    "page with the next_cursor a result ends with."
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to find, in words or names."},
        "mode": {
            "type": "string",
            "enum": [mode.value for mode in SearchMode],
            "description": "auto (default) uses every leg; keyword skips embeddings; "
            "semantic searches by meaning only.",
        },
        "path_glob": {
            "type": "string",
            "description": 'Only paths matching this glob: "backend/**", "**/*.tsx".',
        },
        "lang": {"type": "string", "description": 'A language id: "python", "typescript".'},
        "layer": {"type": "string", "enum": list(LANES), "description": "One layer only."},
        "limit": {"type": "integer", "description": f"Results per page, 1 to {MAX_LIMIT}."},
        "cursor": {"type": "string", "description": "A previous result's next_cursor."},
        "response_format": {
            "type": "string",
            "enum": ["concise", "detailed"],
            "description": "concise (default): the lines around each match; detailed: the "
            "whole definition, up to 60 lines.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

EXAMPLES = (
    {"query": "where are passwords hashed"},
    {"query": "create item", "layer": "api", "path_glob": "backend/**"},
)


def _prefix(glob: str | None) -> str | None:
    """The directory a glob's matches all start in (``backend/app/**/*.py``: ``backend/app/``),
    for the search to narrow by; the glob itself filters what comes back."""
    if not glob:
        return None
    literal: list[str] = []
    for part in glob.split("/"):
        if GLOB_CHARS & set(part):
            break
        literal.append(part)
    if len(literal) == len(glob.split("/")):
        return glob  # no wildcard: a path prefix as it is
    return "/".join(literal) + "/" if literal else None


def _matches(path: str, glob: str | None) -> bool:
    if not glob or not GLOB_CHARS & set(glob):
        return True
    return PurePosixPath(path).full_match(glob)


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    text = str(args.get("query", "")).strip()
    if not text:
        raise ToolError("query is empty.", example='code_search({"query": "create item"})')
    try:
        mode = SearchMode(args.get("mode") or SearchMode.AUTO)
    except ValueError as exc:
        raise ToolError(
            f"mode must be one of {', '.join(m.value for m in SearchMode)}.",
            example='code_search({"query": "create item", "mode": "keyword"})',
        ) from exc
    glob = args.get("path_glob") or None
    layer = args.get("layer") or None
    if layer is not None and layer not in LANES:
        raise ToolError(
            f"layer must be one of {', '.join(LANES)}.",
            example='code_search({"query": "create item", "layer": "api"})',
        )
    limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    detailed = args.get("response_format") == "detailed"
    key = args_key("code_search", args)
    offset = offset_of(args.get("cursor"), key)
    lang = args.get("lang") or None
    query = SearchQuery(text, mode, (lang,) if lang else (), _prefix(glob))
    grep = ctx.grep_legs or WorktreeGrep(ctx.root, search=ctx.exact, files=ctx.searchable)
    try:
        answer = await ctx.search.search(
            ctx.db,
            query,
            repo_id=ctx.repo_id,
            snapshot_id=ctx.snapshot_id,
            scope=str(ctx.org_id),
            grep=grep,
            dense=ctx.dense,
            graph=ctx.graph,
        )
    except SemanticNotReadyError as exc:
        raise ToolError(
            "Semantic search isn't ready for this snapshot yet (embeddings are still being "
            "made). Search with mode auto or keyword.",
            example=f'code_search({{"query": "{text[:40]}", "mode": "keyword"}})',
        ) from exc
    found = sorted(
        (
            r
            for lane, results in answer.lanes.items()
            if layer is None or lane.value == layer
            for r in results
            if _matches(r.path, glob)
        ),
        key=lambda r: (-r.score, r.path, r.start_line),
    )
    page = found[offset : offset + limit]
    out = Results()
    out.text(_interpretation(answer.interpretation.rewrites, answer.semantic_pending))
    chunks = await read_contents(ctx.db, ctx.repo_id, [r.chunk_id for r in page])
    names = await _qualified_names(ctx, [r.symbol_key for r in page if r.symbol_key])
    shown = 0
    for result in page:
        chunk = chunks.get(result.chunk_id)
        if chunk is None:
            continue
        if detailed:
            first, lines = _definition(result, chunk)
        else:
            cut = snippet(result, chunk, answer.terms)
            first, lines = cut.start_line, cut.lines
        if not lines:
            continue
        lines = lines[: max(1, lines_within(lines, out.left))]
        block = code_result(
            ctx,
            result.path,
            first,
            lines,
            title=_title(result, names),
            symbol_key=result.symbol_key,
        )
        if out.code(*block):
            shown += 1
    narrow = "narrow with path_glob, lang or layer"
    out.text(page_note(offset, len(page), len(found), key, exact=True, narrow=narrow))
    files = len({r.path for r in page})
    return out.done(f"{shown} results in {files} files", hits=len(found), narrow=narrow)


async def _qualified_names(ctx: ToolContext, keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = await ctx.db.execute(
        select(CodeNode.symbol_key, CodeNode.qualified_name).where(
            CodeNode.snapshot_id == ctx.snapshot_id, CodeNode.symbol_key.in_(keys)
        )
    )
    return dict(rows.tuples().all())


def _interpretation(rewrites: tuple[str, ...], semantic_pending: bool) -> str:
    said = f"Searched for: {'; '.join(rewrites)}." if rewrites else "Searched as written."
    if semantic_pending:
        said += " (Semantic search isn't ready yet: keyword, symbol and graph legs only.)"
    return said


def _title(result: Result, names: Mapping[str, str]) -> str:
    if result.symbol_key is None:
        return f"{result.path}:{result.start_line}-{result.end_line}"
    qualified = names.get(result.symbol_key, result.name)
    name = readable_id(result.symbol_key, str(result.kind), result.path, qualified)
    return f"{name} ({result.kind}, {result.lane.value})"


def _definition(result: Result, chunk: ChunkText) -> tuple[int, tuple[str, ...]]:
    """The result's definition out of its chunk's lines, its first :data:`DETAILED_LINES`."""
    first = max(result.start_line, chunk.start_line)
    last = min(result.end_line, chunk.start_line + len(chunk.lines) - 1, first + DETAILED_LINES - 1)
    if first > last:  # the result's lines aren't the chunk's: the chunk's own
        first, last = chunk.start_line, chunk.start_line + min(len(chunk.lines), DETAILED_LINES) - 1
    return first, chunk.lines[first - chunk.start_line : last - chunk.start_line + 1]


TOOL = Tool("code_search", DESCRIPTION, SCHEMA, EXAMPLES, run)
