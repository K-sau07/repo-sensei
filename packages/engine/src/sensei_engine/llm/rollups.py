"""Rollups (2.8.5): a summary per directory, written by Claude (the fast tier) from its children's
summaries, deepest directories first, so each one reads its subdirectories' rollups. They seed
the feature maps (4.9); until then they are kept as JSON (:func:`to_json`).

A directory holding nothing but one subdirectory (``src/`` over ``src/app/``) takes that
subdirectory's summary instead of a call of its own. A directory whose call fails is left out
and its parent lists it by name only.

What they will cost is estimated before any summary is asked for (:func:`estimate_cost`), so the
cost guard covers them with the batch.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache
from importlib import resources
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from sensei_core.logging import get_logger
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

log = get_logger(__name__)

PROMPT_VERSION = "summarize_directory.v1"
ROOT = "."
MAX_CHILDREN = 60  # listed per directory; the rest are counted
MAX_CHILD_CHARS = 400  # of each child's summary
MAX_TOKENS = 1_500
CONCURRENCY = 8
# For the estimate: characters per token (as the file summaries' prompt budget assumes), the
# characters around each listed child, and an answer (60 to 120 tokens of summary, its JSON and
# some thinking at low effort), on the high side.
CHARS_PER_TOKEN = 3
CHILD_OVERHEAD_CHARS = 40
ESTIMATED_OUTPUT_TOKENS = 400


@dataclass(frozen=True, slots=True)
class DirectorySummary:
    path: str  # "." for the repository's root
    summary: str
    files: int  # direct children of each kind
    directories: int
    prompt_version: str = PROMPT_VERSION
    inherited_from: str | None = None  # the only subdirectory, when its summary was taken


@dataclass(frozen=True, slots=True)
class RollupEstimate:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(slots=True)
class _Directory:
    files: list[str] = field(default_factory=list[str])
    directories: set[str] = field(default_factory=set[str])


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str


def answer_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }


@cache
def system_prompt() -> str:
    path = resources.files("sensei_engine.llm").joinpath("prompts", f"{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8")


def tree(paths: list[str]) -> dict[str, _Directory]:
    """Every directory above ``paths`` (the root is ``"."``), with its direct children."""
    found: dict[str, _Directory] = {ROOT: _Directory()}
    for path in paths:
        parts = PurePosixPath(path).parts
        parent = ROOT
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            found.setdefault(directory, _Directory())
            found[parent].directories.add(directory)
            parent = directory
        found[parent].files.append(path)
    return found


def _depth(directory: str) -> int:
    return 0 if directory == ROOT else directory.count("/") + 1


def _name(path: str) -> str:
    return PurePosixPath(path).name


def _clip(text: str) -> str:
    return text if len(text) <= MAX_CHILD_CHARS else text[: MAX_CHILD_CHARS - 1] + "…"


def render(
    directory: str,
    files: list[tuple[str, str]],
    subdirectories: list[tuple[str, str | None]],
) -> str:
    """The user message: the directory and its children, each with its summary when there is
    one (at most :data:`MAX_CHILDREN`, summarized ones first)."""
    children = [(f"{name}/", text) for name, text in subdirectories] + list(files)
    children.sort(key=lambda child: child[1] is None)
    lines = [f"<directory>{directory if directory != ROOT else '(repository root)'}</directory>"]
    lines.append("<children>")
    for name, text in children[:MAX_CHILDREN]:
        lines.append(f"{name}: {_clip(text)}" if text else name)
    if len(children) > MAX_CHILDREN:
        lines.append(f"(and {len(children) - MAX_CHILDREN} more)")
    lines.append("</children>")
    return "\n".join(lines)


def _calls(directories: Mapping[str, _Directory]) -> list[str]:
    """The directories that get a call of their own (not the ones holding one subdirectory)."""
    return [
        directory
        for directory, node in directories.items()
        if node.files or len(node.directories) != 1
    ]


def estimate_cost(paths: list[str], calculator: CostCalculator, model: str) -> RollupEstimate:
    """What the rollups above ``paths`` should cost at full price (each child's summary counted
    at its longest), before any of them exists."""
    directories = tree(sorted(paths))
    system = len(system_prompt())
    calls = _calls(directories)
    inputs = [
        math.ceil(
            (
                system
                + len(directory)
                + min(len(node.files) + len(node.directories), MAX_CHILDREN)
                * (MAX_CHILD_CHARS + CHILD_OVERHEAD_CHARS)
            )
            / CHARS_PER_TOKEN
        )
        for directory in calls
        for node in (directories[directory],)
    ]
    costs = [
        calculator.cost(PROVIDER, model, tokens_in=tokens, tokens_out=ESTIMATED_OUTPUT_TOKENS)
        for tokens in inputs
    ]
    if any(cost is None for cost in costs):
        msg = f"no price for {model}: the cost guard can't estimate"
        raise ValueError(msg)
    total = sum((cost for cost in costs if cost is not None), Decimal(0))
    return RollupEstimate(len(calls), sum(inputs), ESTIMATED_OUTPUT_TOKENS * len(calls), total)


async def summarize_directories(
    client: AnthropicClient,
    file_summaries: Mapping[str, str],
    *,
    on_usage: LLMUsageSink | None = None,
    concurrency: int = CONCURRENCY,
) -> dict[str, DirectorySummary]:
    """A summary for every directory above the summarized files (``file_summaries``: path ->
    summary), by path."""
    directories = tree(sorted(file_summaries))
    done: dict[str, DirectorySummary] = {}
    gate = asyncio.Semaphore(concurrency)

    async def one(directory: str) -> None:
        node = directories[directory]
        subdirectories = sorted(node.directories)
        if not node.files and len(subdirectories) == 1:
            only = done.get(subdirectories[0])
            if only is not None:
                done[directory] = DirectorySummary(
                    directory, only.summary, 0, 1, inherited_from=only.path
                )
            return
        files = [(_name(path), file_summaries[path]) for path in sorted(node.files)]
        subs = [(_name(d), done[d].summary if d in done else None) for d in subdirectories]
        request = LLMRequest(
            render(directory, files, subs),
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
                summary = " ".join(_Answer.model_validate(result.data).summary.split())
            except (LLMError, ValidationError) as exc:
                log.warning("directory rollup skipped", directory=directory, error=str(exc))
                return
        if summary:
            done[directory] = DirectorySummary(directory, summary, len(files), len(subdirectories))

    for depth in sorted({_depth(d) for d in directories}, reverse=True):
        level = [d for d in directories if _depth(d) == depth]
        await asyncio.gather(*(one(directory) for directory in level))
    return dict(sorted(done.items()))


def to_json(rollups: Mapping[str, DirectorySummary]) -> str:
    """The rollups as a JSON artifact (feature map seeds until 4.9 gives them a table)."""
    return json.dumps(
        {
            "prompt_version": PROMPT_VERSION,
            "directories": [
                {
                    "path": rollup.path,
                    "summary": rollup.summary,
                    "files": rollup.files,
                    "directories": rollup.directories,
                    "inherited_from": rollup.inherited_from,
                }
                for rollup in rollups.values()
            ],
        },
        indent=2,
        ensure_ascii=False,
    )
