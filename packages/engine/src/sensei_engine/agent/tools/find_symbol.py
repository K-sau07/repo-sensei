"""``find_symbol``: where a name is defined (the symbols index, as the code browser's symbol
search finds it): exact names first, then prefixes, parts of qualified names and near misses."""

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
    readable_id,
)
from sensei_engine.symbols.extract import SymbolKind
from sensei_engine.symbols.lookup import SymbolHit, find_symbols

LIMIT = 10
HEAD_LINES = 20  # a definition's first lines, in the detailed form

DESCRIPTION = (
    "Find where a function, class, method, React component, hook or variable is defined, by "
    "its name (create_item, ItemsService, useAuth) or part of its qualified name "
    "(ItemsService.createItem). Use it when you know or can guess a name: it is faster and "
    "surer than code_search for that. Each match gives its symbol id (sym:<path>::<name>), "
    "kind, file and lines and signature; exact names come first, then prefixes, then near "
    "misses. Pass a match's symbol id to graph_neighbors or trace_feature, or its lines to "
    "read_code. With response_format detailed, each match also shows its definition's first "
    "lines, citable. Don't use it for text in the code (use grep) or a question in words "
    "(use code_search)."
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The name, or part of a qualified name."},
        "kind": {
            "type": "string",
            "enum": [kind.value for kind in SymbolKind],
            "description": "Only definitions of this kind.",
        },
        "response_format": {
            "type": "string",
            "enum": ["concise", "detailed"],
            "description": "concise (default): ids, places and signatures; detailed: also "
            "each definition's first lines.",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

EXAMPLES = ({"name": "create_item"}, {"name": "useAuth", "kind": "hook"})


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    name = str(args.get("name", "")).strip()
    if not name:
        raise ToolError("name is empty.", example='find_symbol({"name": "create_item"})')
    kind = None
    if args.get("kind"):
        try:
            kind = SymbolKind(args["kind"])
        except ValueError as exc:
            raise ToolError(
                f"kind must be one of {', '.join(k.value for k in SymbolKind)}.",
                example='find_symbol({"name": "useAuth", "kind": "hook"})',
            ) from exc
    hits = await find_symbols(ctx.db, ctx.snapshot_id, name, kind=kind, limit=LIMIT)
    out = Results()
    if not hits:
        out.text(
            f"No definition is named like {name!r}. Try part of the name, another kind, or "
            "code_search for the code in words."
        )
        return out.done("no definitions", hits=0, narrow="")
    out.text("\n".join(_line(n, hit) for n, hit in enumerate(hits, start=1)))
    if args.get("response_format") == "detailed":
        for hit in hits:
            lines = file_lines(ctx, hit.path, example=f'read_code({{"path": "{hit.path}"}})')
            head = lines[hit.start_line - 1 : min(hit.end_line, hit.start_line + HEAD_LINES - 1)]
            if head:
                title = _id(hit)
                out.code(
                    *code_result(
                        ctx, hit.path, hit.start_line, head, title=title, symbol_key=hit.symbol_key
                    )
                )
    narrow = "ask for fewer with a fuller name or a kind"
    return out.done(f"{len(hits)} definitions", hits=len(hits), narrow=narrow)


def _id(hit: SymbolHit) -> str:
    return readable_id(hit.symbol_key, hit.kind, hit.path, hit.qualified_name)


def _line(n: int, hit: SymbolHit) -> str:
    signature = f"  {hit.signature}" if hit.signature else ""
    return f"{n}. {_id(hit)}  ({hit.kind})  {hit.path}:{hit.start_line}-{hit.end_line}{signature}"


TOOL = Tool("find_symbol", DESCRIPTION, SCHEMA, EXAMPLES, run)
