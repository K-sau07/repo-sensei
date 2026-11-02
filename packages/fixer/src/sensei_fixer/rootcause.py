"""The root cause, once the bug is reproduced (8.7): what the investigation found, with what the
graph says depends on it.

No model: the root cause's lines are mapped to the graph's definitions there, and its **blast
radius** (:func:`sensei_engine.impact.impact`: the routes, screens, tests, tables and other code
that depend on them) and the sensitive areas it touches (:mod:`.risk`) are added to it. Whether
the reproducing test's failure runs through the root cause's file is noted too (its output names
the file): the approval card shows it, a reviewer weighs it. The plan and its risk start here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, cast

from sensei_core.models import FixStatus
from sensei_fixer.reproduce import Workspace
from sensei_fixer.risk import Areas, graph_areas, path_areas, text_areas
from sensei_fixer.stages import Advance, Outcome, StageContext, StageHandler, Step, Stop


def seen_in(runs: Sequence[Mapping[str, Any]], path: str) -> bool:
    """A run's output names ``path`` (the failure passed through the file)."""
    name = PurePosixPath(path).name
    outputs = [str(r.get("output", "")) for r in runs]
    return any(path in out or f'/{name}"' in out or f"{name}:" in out for out in outputs)


def root_cause_stage(workspace: Workspace) -> StageHandler:
    """The pipeline's ``root_cause`` handler."""

    async def handler(ctx: StageContext) -> Outcome:
        found = cast("Mapping[str, Any] | None", ctx.facts.get("root_cause"))
        repro = cast("Mapping[str, Any]", ctx.facts.get("repro") or {})
        if not found or not found.get("path"):
            return Stop(FixStatus.FAILED, "There is no root cause to build on.")
        path = str(found["path"])
        lines = (int(found.get("start_line") or 1), int(found.get("end_line") or 1))
        areas = Areas()
        path_areas([path], areas)
        text_areas([("the root cause", str(found.get("claim") or ""))], areas)
        async with workspace(ctx) as (tools, _history):
            if path not in tools.tree:
                return Stop(FixStatus.FAILED, f"The root cause's file {path} isn't in the "
                            "repository at the commit being fixed.")  # fmt: skip
            blast = graph_areas(tools.graph, [path], areas, lines={path: lines})
        runs = cast("Sequence[Mapping[str, Any]]", repro.get("runs") or ())
        seen = seen_in(runs, path)
        counts = cast("Mapping[str, int]", (blast or {}).get("counts") or {})
        said = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "nothing found"
        await ctx.record(Step("tool", "blast radius", said if blast else "no graph yet"))
        return Advance({"root_cause": {
            **found, "blast_radius": blast, "areas": dict(areas.found), "seen_in_repro": seen,
        }})  # fmt: skip

    return handler
