"""``submit_evidence``: the code the agent commits to answering from, before it answers (4.4
reranks it with everything the run read). Each piece is checked against the commit: a symbol
that exists, lines its file has."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import select

from sensei_core.models import CodeNode, RepoFile
from sensei_engine.agent.tools.base import (
    LANES,
    Evidence,
    Results,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    file_lines,
    file_path,
    symbol_key_of,
)

MAX_CANDIDATES = 20
EXAMPLE = (
    'submit_evidence({"candidates": [{"target": "sym:backend/app/crud.py::create_item", '
    '"layer": "svc", "why": "saves the new item"}]})'
)

DESCRIPTION = (
    "Commit to the evidence your answer rests on, before you write the answer: for each piece, "
    "the symbol id or file path, the lines (a symbol's own by default), its layer (ui, api, "
    "svc, db, other) and in a few words why it answers the question. Call it once, when you "
    "have read enough, with every piece you will cite (at most 20), best first; call it again "
    "only to add what a later search found. Only code you read in this run belongs here. Each "
    "piece is checked: one whose symbol, file or lines don't exist at this commit is refused, "
    "with the reason."
)

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "A symbol id or a file path."},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "layer": {"type": "string", "enum": list(LANES)},
                    "why": {"type": "string", "description": "Why it answers the question."},
                },
                "required": ["target", "layer", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

EXAMPLES = (
    {
        "candidates": [
            {
                "target": "sym:backend/app/api/routes/items.py::create_item",
                "layer": "api",
                "why": "the endpoint that creates an item",
            },
            {
                "target": "backend/app/models.py",
                "start_line": 40,
                "end_line": 52,
                "layer": "db",
                "why": "the item table it writes",
            },
        ]
    },
)


async def run(ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    given: object = args.get("candidates")
    if not isinstance(given, list) or not given:
        raise ToolError(
            "candidates is empty: give the pieces of code you will cite.", example=EXAMPLE
        )
    candidates = cast("list[object]", given)
    if len(candidates) > MAX_CANDIDATES:
        raise ToolError(f"At most {MAX_CANDIDATES} pieces; keep the best ones.", example=EXAMPLE)
    kept: list[Evidence] = []
    refused: list[str] = []
    for n, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            refused.append(f"{n}. not an object with a target, a layer and a why.")
            continue
        try:
            kept.append(await _checked(ctx, cast("dict[str, Any]", candidate)))
        except ToolError as exc:
            refused.append(f"{n}. {exc.message}")
    held = {(e.path, e.start_line, e.end_line) for e in ctx.evidence}
    new = [e for e in kept if (e.path, e.start_line, e.end_line) not in held]
    ctx.evidence.extend(new)
    out = Results()
    lines = [f"Recorded {len(new)} pieces of evidence ({len(ctx.evidence)} in all)."]
    lines += [f"- {e.target}  {e.path}:{e.start_line}-{e.end_line}  ({e.layer})" for e in new]
    if refused:
        lines.append("Refused:")
        lines += refused
    out.text("\n".join(lines))
    return out.done(f"{len(new)} recorded, {len(refused)} refused", hits=len(new), narrow="")


async def _checked(ctx: ToolContext, candidate: Mapping[str, Any]) -> Evidence:
    target = str(candidate.get("target", "")).strip()
    layer = str(candidate.get("layer", ""))
    why = str(candidate.get("why", "")).strip()
    if not target:
        raise ToolError("target is empty.")
    if layer not in LANES:
        raise ToolError(f"{target}: layer must be one of {', '.join(LANES)}.")
    start, end = candidate.get("start_line"), candidate.get("end_line")
    if target.startswith("sym:") or " " in target:
        key = await symbol_key_of(ctx, target)
        found = (
            await ctx.db.execute(
                select(RepoFile.path, CodeNode.start_line, CodeNode.end_line)
                .join(RepoFile, RepoFile.id == CodeNode.file_id)
                .where(CodeNode.snapshot_id == ctx.snapshot_id, CodeNode.symbol_key == key)
                .limit(1)
            )
        ).first()
        if found is None:
            raise ToolError(f"{target} is not a symbol at this commit.")
        path, own_start, own_end = found
        start, end = int(start or own_start), int(end or own_end)
    else:
        path = file_path(ctx, target, example=EXAMPLE)
        if start is None:
            raise ToolError(f"{target}: a file needs start_line (and end_line).")
        start = int(start)
        end = int(end or start)
    length = len(file_lines(ctx, path, example=EXAMPLE))
    if not 1 <= start <= end <= length:
        raise ToolError(f"{target}: lines {start}-{end} aren't in {path} ({length} lines).")
    return Evidence(target, path, start, end, layer, why)


TOOL = Tool("submit_evidence", DESCRIPTION, SCHEMA, EXAMPLES, run)
