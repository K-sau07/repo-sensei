"""Feature maps (4.9): what a module of the repository is for, its entry points, its key symbols
and how data flows through it, written once per index from the summaries (2.8), for questions
about a part of the repository as a whole (the agent's ``get_feature_map``).

A module is a directory with at least :data:`MIN_FILES` summarized files beneath it, the
largest first, at most :data:`MAX_MAPS`. The fast model reads the directory's rollup and its
files' summaries, and picks the entry points and key symbols among numbered candidates (the
symbols the summaries describe): every symbol a map names is one the index has.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.logging import get_logger
from sensei_core.models import CodeNode, FeatureMap, RepoFile
from sensei_core.usage import CostCalculator
from sensei_engine.llm.anthropic_client import (
    PROVIDER,
    AnthropicClient,
    LLMError,
    LLMRequest,
    LLMUsageSink,
    Role,
    tier_for,
)
from sensei_engine.llm.summaries import FileSummary

log = get_logger(__name__)

PROMPT_VERSION = "feature_map.v1"
MIN_FILES = 2
MAX_MAPS = 24
MAX_FILES = 40  # of a module, read (the rest counted)
MAX_SYMBOLS = 120  # candidates offered
MAX_CHARS = 400  # of a summary, read
MAX_TOKENS = 2_000
CONCURRENCY = 6
CHARS_PER_TOKEN = 3
ESTIMATED_OUTPUT_TOKENS = 700


@dataclass(frozen=True, slots=True)
class Picked:
    symbol_key: str
    why: str


@dataclass(frozen=True, slots=True)
class FeatureMapDraft:
    key: str  # "dir:backend/app/api"
    path: str
    title: str
    summary: str
    entry_points: tuple[Picked, ...]
    key_symbols: tuple[Picked, ...]
    data_flow: tuple[str, ...]
    files: tuple[str, ...]  # the module's summarized files


class ModelPick(BaseModel):
    id: str
    why: str


class _Answer(BaseModel):
    title: str
    summary: str
    entry_points: list[ModelPick]
    key_symbols: list[ModelPick]
    data_flow: list[str]


def answer_schema() -> dict[str, Any]:
    pick = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "why": {"type": "string"}},
        "required": ["id", "why"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "entry_points": {"type": "array", "items": pick},
            "key_symbols": {"type": "array", "items": pick},
            "data_flow": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "summary", "entry_points", "key_symbols", "data_flow"],
        "additionalProperties": False,
    }


def system_prompt() -> str:
    path = resources.files("sensei_engine.llm").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


def modules(paths: Sequence[str]) -> list[tuple[str, list[str]]]:
    """The modules to map: each directory with at least :data:`MIN_FILES` of ``paths`` beneath
    it (not the root, unless it is all there is), the largest first (then by path), at most
    :data:`MAX_MAPS`; with its files."""
    beneath: dict[str, list[str]] = {}
    for path in sorted(paths):
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            beneath.setdefault("/".join(parts[:depth]), []).append(path)
    found = [(d, files) for d, files in beneath.items() if len(files) >= MIN_FILES]
    if not found and len(paths) >= MIN_FILES:
        found = [(".", sorted(paths))]
    found.sort(key=lambda item: (-len(item[1]), item[0]))
    return found[:MAX_MAPS]


def render(
    directory: str, rollup: str | None, files: Sequence[FileSummary]
) -> tuple[str, list[str]]:
    """The prompt for one module, and its candidates' symbol keys (``s1`` is the first)."""
    lines = [f"Module: {directory}/", f"What it holds, in brief: {rollup or '(no summary)'}", ""]
    candidates: list[str] = []
    shown = files[:MAX_FILES]
    for summary in shown:
        lines.append(f"File {summary.path}: {_clip(summary.summary)}")
        for key, symbol in summary.symbols.items():
            if len(candidates) >= MAX_SYMBOLS:
                break
            candidates.append(key)
            lines.append(f"  s{len(candidates)} {_name(key)}: {_clip(symbol.summary)}")
    if len(files) > len(shown):
        lines.append(f"({len(files) - len(shown)} more files)")
    return "\n".join(lines), candidates


def estimate_cost(
    drafts: Sequence[tuple[str, list[str]]],
    summaries: Mapping[str, FileSummary],
    calculator: CostCalculator,
    model: str,
) -> Decimal:
    """About what mapping ``drafts`` (modules and their files) costs."""
    tokens = 0
    for _, files in drafts:
        chars = sum(len(summaries[p].summary) for p in files[:MAX_FILES] if p in summaries)
        chars += sum(
            len(s.summary)
            for p in files[:MAX_FILES]
            if p in summaries
            for s in summaries[p].symbols.values()
        )
        tokens += min(chars, (MAX_FILES + MAX_SYMBOLS) * MAX_CHARS) // CHARS_PER_TOKEN + 600
    cost = calculator.cost(
        PROVIDER,
        model,
        tokens_in=tokens,
        tokens_out=ESTIMATED_OUTPUT_TOKENS * len(drafts),
    )
    return cost or Decimal(0)


async def generate(
    client: AnthropicClient,
    summaries: Mapping[str, FileSummary],
    rollups: Mapping[str, str],
    *,
    on_usage: LLMUsageSink | None = None,
    concurrency: int = CONCURRENCY,
) -> list[FeatureMapDraft]:
    """A map for each module (:func:`modules`); one the model can't write is left out."""
    gate = asyncio.Semaphore(concurrency)

    async def one(directory: str, paths: list[str]) -> FeatureMapDraft | None:
        files = [summaries[path] for path in paths if path in summaries]
        prompt, candidates = render(directory, rollups.get(directory), files)
        request = LLMRequest(
            prompt,
            system=system_prompt(),
            tier=tier_for(Role.SUMMARY),
            max_tokens=MAX_TOKENS,
            effort="low",
            schema=answer_schema(),
            cache_system=True,
        )
        async with gate:
            try:
                result = await client.complete(request, on_usage=on_usage)
                answer = _Answer.model_validate(result.data)
            except (LLMError, ValidationError) as exc:
                log.warning("feature map skipped", directory=directory, error=str(exc))
                return None
        return FeatureMapDraft(
            f"dir:{directory}",
            directory,
            " ".join(answer.title.split()) or directory,
            " ".join(answer.summary.split()),
            picked(answer.entry_points, candidates),
            picked(answer.key_symbols, candidates),
            tuple(" ".join(step.split()) for step in answer.data_flow if step.strip()),
            tuple(paths),
        )

    found = await asyncio.gather(
        *(one(directory, paths) for directory, paths in modules(list(summaries)))
    )
    return [draft for draft in found if draft is not None]


def picked(picks: Sequence[ModelPick], candidates: Sequence[str]) -> tuple[Picked, ...]:
    """The picks that name a candidate (``s3``), each once, in order."""
    seen: set[str] = set()
    out: list[Picked] = []
    for pick in picks:
        handle = pick.id.strip().lower().removeprefix("s")
        if not handle.isdigit() or not 1 <= int(handle) <= len(candidates):
            continue
        key = candidates[int(handle) - 1]
        if key not in seen:
            seen.add(key)
            out.append(Picked(key, " ".join(pick.why.split())))
    return tuple(out)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def _name(symbol_key: str) -> str:
    """``py a/b.py/Item#save().`` → ``Item.save`` (for reading; the key is what is kept)."""
    _, _, rest = symbol_key.partition(" ")
    tail = rest.rsplit("/", 1)[-1] or rest
    return ".".join(p for p in tail.replace("().", ".").replace("#", ".").split(".") if p) or tail


# ---------------------------------------------------------------- stored


async def store(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    drafts: Sequence[FeatureMapDraft],
) -> int:
    """The snapshot's maps (any it had replaced), each symbol with its lines, and the content
    hashes of the module's files: how many."""
    await db.execute(delete(FeatureMap).where(FeatureMap.snapshot_id == snapshot_id))
    keys = {p.symbol_key for d in drafts for p in (*d.entry_points, *d.key_symbols)}
    lines: dict[str, tuple[str, int, int]] = {}
    if keys:
        rows = await db.execute(
            select(CodeNode.symbol_key, RepoFile.path, CodeNode.start_line, CodeNode.end_line)
            .join(RepoFile, RepoFile.id == CodeNode.file_id)
            .where(CodeNode.snapshot_id == snapshot_id, CodeNode.symbol_key.in_(sorted(keys)))
        )
        lines = {key: (path, start, end) for key, path, start, end in rows.tuples()}
    for draft in drafts:
        entry = _placed(draft.entry_points, lines)
        symbols = _placed(draft.key_symbols, lines)
        db.add(
            FeatureMap(
                org_id=org_id,
                repo_id=repo_id,
                snapshot_id=snapshot_id,
                key=draft.key,
                path=draft.path,
                title=draft.title,
                summary=draft.summary,
                entry_points=entry,
                key_symbols=symbols,
                data_flow=list(draft.data_flow),
                citations=[
                    {"path": s["path"], "start_line": s["start_line"], "end_line": s["end_line"]}
                    for s in (*entry, *symbols)
                ],
                file_hashes=await _hashes(db, snapshot_id, draft.files),
            )
        )
    await db.flush()
    return len(drafts)


async def count(db: AsyncSession, snapshot_id: uuid.UUID) -> int:
    found = await db.scalar(
        select(func.count()).select_from(FeatureMap).where(FeatureMap.snapshot_id == snapshot_id)
    )
    return int(found or 0)


async def find(
    db: AsyncSession, snapshot_id: uuid.UUID, wanted: str, *, limit: int = 3
) -> list[FeatureMap]:
    """The snapshot's maps for ``wanted``: a module's path (the deepest module holding it), else
    the maps whose title, summary or path share its words, most shared first."""
    maps = list(await db.scalars(select(FeatureMap).where(FeatureMap.snapshot_id == snapshot_id)))
    path = wanted.strip().strip("/")
    holding = [m for m in maps if path == m.path or path.startswith(f"{m.path}/")]
    if holding:
        return sorted(holding, key=lambda m: -len(m.path))[:1]
    words = {w for w in re.split(r"[^a-z0-9]+", wanted.lower()) if len(w) >= 3}
    scored: list[tuple[int, str, FeatureMap]] = []
    for found in maps:
        text = f"{found.title} {found.summary} {found.path}".lower()
        shared = sum(word in text for word in words)
        if shared:
            scored.append((-shared, found.path, found))
    return [found for _, _, found in sorted(scored, key=lambda item: item[:2])][:limit]


async def current(db: AsyncSession, found: FeatureMap) -> bool:
    """Whether the map's files still have the content it was written from (else it is marked
    stale, to be written again)."""
    now = await _hashes(db, found.snapshot_id, sorted(found.file_hashes))
    if now == found.file_hashes:
        return True
    found.stale = True
    await db.flush()
    return False


def _placed(
    picked: Sequence[Picked], lines: Mapping[str, tuple[str, int, int]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pick in picked:
        where = lines.get(pick.symbol_key)
        if where is None:
            continue
        path, start, end = where
        out.append(
            {"symbol_key": pick.symbol_key, "why": pick.why, "path": path,
             "start_line": start, "end_line": end}
        )  # fmt: skip
    return out


async def _hashes(db: AsyncSession, snapshot_id: uuid.UUID, paths: Sequence[str]) -> dict[str, str]:
    if not paths:
        return {}
    rows = await db.execute(
        select(RepoFile.path, RepoFile.content_hash).where(
            RepoFile.snapshot_id == snapshot_id, RepoFile.path.in_(sorted(paths))
        )
    )
    return {path: digest.hex() for path, digest in rows.tuples()}
