"""Round 0 (4.3): the evidence a question starts with, found before the model's first turn, so
most answers need fewer tool calls.

The question is searched as ``code_search`` searches it (routed, hybrid, reranked); a question
about a flow is also traced from its best page or endpoint. The results go into the first user
turn as citable search results, grouped by layer (ui, api, svc, db, other) and, within one, by
file (its best result first, its results in line order): a map of where the answer is, as well
as its first evidence. All of it fits in :data:`ROUND0_TOKENS`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from sensei_engine.agent.tools import ToolContext, ToolError
from sensei_engine.agent.tools.base import LANES, Results, Source
from sensei_engine.agent.tools.code_search import add_results, search
from sensei_engine.agent.tools.trace_feature import describe_path
from sensei_engine.agent.verify import agreement
from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.graph.trace import TraceError, trace_feature
from sensei_engine.search.route import QueryClass, Route
from sensei_engine.search.service import Answer, Result, SearchQuery
from sensei_engine.symbols.extract import SymbolKind

ROUND0_TOKENS = 12_000
AGREEMENT_TOP = 8  # the spans two passes are compared by
TWO_PASSES = frozenset({QueryClass.FLOW, QueryClass.ARCHITECTURE})
MAX_RESULTS = 16
SEED_LANES = ("ui", "api")  # where a flow is traced from: its page, else its endpoint
FLOW_KINDS = frozenset(
    kind.value
    for kind in (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.COMPONENT, SymbolKind.HOOK)
)
INTRO = (
    "Code that matched the question, found before you started: grouped by layer, then by "
    "file, best first. Read on with the tools where it isn't enough."
)


@dataclass(frozen=True, slots=True)
class Round0:
    """The blocks for the first turn (before the question), the source of each search result
    among them in order, and what was found."""

    content: tuple[dict[str, Any], ...]
    sources: tuple[Source, ...]
    results: int
    files: int
    traced: int  # trace paths
    ms: int
    agreement: float | None = None  # of the two passes, when it took two (4.5)


async def pre_retrieve(ctx: ToolContext, question: str) -> Round0 | None:
    """Round 0's evidence for ``question``; None when nothing was found, or the search can't
    run (the run then starts with the question alone)."""
    started = time.monotonic()
    route = await ctx.search.route(question, scope=str(ctx.org_id))
    passes = _passes(route, question)
    try:
        answers = [await search(ctx, SearchQuery(text)) for text in passes]
    except ToolError:
        return None
    ranked = [_ranked(answer) for answer in answers]
    best = _interleaved(ranked)[:MAX_RESULTS]
    if not best:
        return None
    terms = tuple(dict.fromkeys(term for answer in answers for term in answer.terms))
    agreed = None
    if len(ranked) == 2:
        first, second = ([(r.path, r.start_line) for r in one[:AGREEMENT_TOP]] for one in ranked)
        agreed = agreement(first, second)
    out = Results(ROUND0_TOKENS)
    out.text(INTRO)
    paths = await _traced(ctx, question, best) if route.query_class is QueryClass.FLOW else ()
    for n, path in enumerate(paths, start=1):
        out.text(path if n > 1 else f"The flow, traced from its best match:\n{path}")
    shown = 0
    for lane in LANES:
        grouped = _by_file([r for r in best if r.lane.value == lane])
        if grouped and out.text(f"Layer {lane}:"):
            shown += (await add_results(ctx, out, grouped, terms))[0]
    if not shown:
        return None
    result = out.done("", hits=shown, narrow="search with the tools for more")
    ms = round((time.monotonic() - started) * 1000)
    files = len({source.path for source in result.sources})
    return Round0(
        tuple(result.content), tuple(result.sources), shown, files, len(paths), ms, agreed
    )


def _passes(route: Route, question: str) -> list[str]:
    """What round 0 searches for: a flow or architecture question in its two decompositions
    (what the code is, what it does), when the router gave both; else the question."""
    structural, behavioural = route.structural_rewrite, route.behavioural_rewrite
    if route.query_class in TWO_PASSES and structural and behavioural:
        return [structural, behavioural]
    return [question]


def _ranked(answer: Answer) -> list[Result]:
    return sorted(
        (result for results in answer.lanes.values() for result in results),
        key=lambda r: (-r.score, r.path, r.start_line),
    )


def _interleaved(ranked: Sequence[Sequence[Result]]) -> list[Result]:
    """Passes' results taken in turn (their scores aren't on one scale), each span once."""
    seen: set[tuple[str, int, int]] = set()
    merged: list[Result] = []
    for row in zip_longest(*ranked):
        for result in row:
            if result is None:
                continue
            key = (result.path, result.start_line, result.end_line)
            if key not in seen:
                seen.add(key)
                merged.append(result)
    return merged


def _by_file(results: Sequence[Result]) -> list[Result]:
    """Results by file (the file of the best result first), each file's in line order."""
    files: dict[str, list[Result]] = {}
    for result in results:  # best first
        files.setdefault(result.path, []).append(result)
    return [r for found in files.values() for r in sorted(found, key=lambda r: r.start_line)]


def _definition(graph: GraphSnapshot, result: Result) -> str | None:
    """The definition a result is, or for a whole file (a module) its first one: where a flow
    starts."""
    if result.kind != SymbolKind.MODULE:
        return result.symbol_key
    inside = [node for node in graph.nodes if node.path == result.path and node.kind in FLOW_KINDS]
    return min(inside, key=lambda n: n.start_line).symbol_key if inside else None


async def _traced(ctx: ToolContext, question: str, best: Sequence[Result]) -> tuple[str, ...]:
    """The flow from the best page (else endpoint) definition that matched, as the trace tool
    shows it: its paths of more than one hop."""
    if ctx.graph is None:
        return ()
    graph = ctx.graph
    seeds = (
        _definition(graph, r)
        for lane in SEED_LANES
        for r in best
        if r.lane.value == lane and r.symbol_key
    )
    seed = next((key for key in seeds if key is not None), None)
    if seed is None:
        return ()
    try:
        traced = await asyncio.to_thread(
            trace_feature, graph, seed, direction="down", feature=question
        )
    except TraceError:
        return ()
    paths = [path for path in traced.paths if len(path.hops) > 1]  # one hop is no flow
    return tuple(describe_path(n, path) for n, path in enumerate(paths, start=1))
