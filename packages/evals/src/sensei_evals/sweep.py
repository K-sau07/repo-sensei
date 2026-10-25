"""The weight sweep of step 2.9.4, and the leg comparison of 2.9.5.

Every question's rankings are read once (the full plan of each: every leg the query could get,
:func:`~sensei_evals.hybrid.plan_for`), with the chunks they found. Fusing is then pure, so each
point of the grid costs no query: per point, every item is fused, arranged and scored as a run
scores it (:func:`~sensei_evals.runner.score_item`).

Three ways to read the same rankings:

* ``plan``: what search runs (a symbol or pattern query's short-circuit only);
* ``full``: every leg of every query;
* one leg: only that leg's rankings of the full plan (a leg measured on its own).
"""

from __future__ import annotations

import dataclasses
import itertools
import statistics
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sensei_engine.search.hybrid import (
    ChunkDetails,
    Ranking,
    SymbolDetails,
    Weights,
    arrange,
    fuse,
    read_chunks,
    read_symbols,
)
from sensei_engine.search.plan import Leg, SearchPlan
from sensei_evals.hybrid import HybridEvalRetriever, plan_for, to_hit
from sensei_evals.retrieval import RepoRef
from sensei_evals.runner import score_item
from sensei_evals.schema import GoldenItem

GATE = "recall_symbol@20"
REPORTED = ("recall_symbol@20", "recall_symbol@10", "recall_file@20", "ndcg_symbol@10")
KS = (10, 20)
DEPTH = 100  # what a run reads: its largest k (each leg as deep)
SINGLE_LEGS = (Leg.LEX_CODE, Leg.LEX_PATH, Leg.SYM, Leg.GREP)


@dataclass(frozen=True, slots=True)
class Collected:
    item: GoldenItem
    plan: SearchPlan  # the full plan
    searched: SearchPlan  # the plan search runs
    rankings: Sequence[Ranking]  # of both plans' sub-queries
    details: Mapping[uuid.UUID, ChunkDetails]
    symbols: Mapping[str, SymbolDetails]  # the definitions found in chunks of several


async def collect(
    retriever: HybridEvalRetriever, items: Sequence[GoldenItem], repo: RepoRef
) -> list[Collected]:
    """Every item's rankings and chunks (the retriever's index built first; closed after)."""
    try:
        return await _collect(retriever, items, repo)
    finally:
        await retriever.aclose()


async def _collect(
    retriever: HybridEvalRetriever, items: Sequence[GoldenItem], repo: RepoRef
) -> list[Collected]:
    await retriever.prepare(repo)
    ids = retriever.snapshot
    if ids is None:  # pragma: no cover - prepare set it or raised
        return []
    repo_id, snapshot_id = ids
    hybrid = retriever.hybrid
    collected: list[Collected] = []
    for item in items:
        full = plan_for(item.query, full=True)
        searched = plan_for(item.query)
        # A short-circuit's sub-queries aren't all the full plan's: both plans' are read.
        extra = tuple(sub for sub in searched.legs if sub not in full.legs)
        both = dataclasses.replace(full, legs=(*full.legs, *extra))
        async with retriever.sessions() as db:
            rankings, _ = await hybrid.rankings(
                db, both, repo_id=repo_id, snapshot_id=snapshot_id, k=DEPTH
            )
            chunk_ids = {ref.chunk_id for ranking in rankings for ref in ranking.found}
            details = await read_chunks(db, repo_id, sorted(chunk_ids))
            focus = [ref.focus for ranking in rankings for ref in ranking.found if ref.focus]
            symbols = await read_symbols(db, snapshot_id, focus)
        collected.append(Collected(item, full, searched, rankings, details, symbols))
    return collected


def _kept(entry: Collected, view: str) -> tuple[SearchPlan, list[Ranking]]:
    """The plan a view arranges by, and its rankings."""
    if view == "plan":
        return entry.searched, [r for r in entry.rankings if r.sub in entry.searched.legs]
    full = [r for r in entry.rankings if r.sub in entry.plan.legs]
    if view == "full":
        return entry.plan, full
    return entry.plan, [r for r in full if r.sub.leg.value == view]


def evaluate(collected: Iterable[Collected], weights: Weights, view: str) -> dict[str, float]:
    """The average of each reported metric (over the items it applies to)."""
    scores: dict[str, list[float]] = {name: [] for name in REPORTED}
    for entry in collected:
        plan, rankings = _kept(entry, view)
        hits = [
            to_hit(hit)
            for hit in arrange(
                plan,
                fuse(rankings, weights),
                entry.details,
                weights,
                symbols=entry.symbols,
            )
        ][: max(KS)]
        item_scores = score_item(entry.item, hits, KS)
        for name in REPORTED:
            value = item_scores.get(name)
            if value is not None:
                scores[name].append(value)
    return {name: round(statistics.fmean(v), 4) if v else 0.0 for name, v in scores.items()}


GRID: Mapping[str, Sequence[float]] = {
    "lex_code": (0.6, 1.0, 1.4),
    "lex_path": (0.4, 0.8, 1.2),
    "sym": (0.6, 1.2, 1.8),
    "grep": (0.0, 0.5, 1.0),
    "multi_hit": (0.0, 0.1, 0.3),
    "layer_boost": (1.0, 1.2),
}


def grid(base: Weights, values: Mapping[str, Sequence[float]] = GRID) -> Iterable[Weights]:
    names = list(values)
    for point in itertools.product(*(values[name] for name in names)):
        chosen = dict(zip(names, point, strict=True))
        legs = dict(base.legs)
        for leg in (Leg.LEX_CODE, Leg.LEX_PATH, Leg.SYM, Leg.GREP):
            if leg.value in chosen:
                legs[leg] = chosen[leg.value]
        yield dataclasses.replace(
            base,
            legs=legs,
            multi_hit=chosen.get("multi_hit", base.multi_hit),
            layer_boost=chosen.get("layer_boost", base.layer_boost),
        )


def describe(weights: Weights) -> str:
    legs = ", ".join(f"{leg.value}={weights.legs[leg]:g}" for leg in SINGLE_LEGS)
    return f"{legs}, multi_hit={weights.multi_hit:g}, layer_boost={weights.layer_boost:g}"


@dataclass(frozen=True, slots=True)
class Point:
    weights: Weights
    scores: Mapping[str, Mapping[str, float]]  # suite -> metric -> average

    def mean(self, metric: str = GATE) -> float:
        return statistics.fmean(scores[metric] for scores in self.scores.values())


def sweep(
    collected: Mapping[str, Sequence[Collected]], base: Weights, view: str = "plan"
) -> list[Point]:
    """Every grid point scored on every suite, best first by the mean of :data:`GATE` (ties:
    the mean nDCG, then the point nearest ``base``: a weight no score moves keeps its prior)."""
    points = [
        Point(
            weights,
            {suite: evaluate(entries, weights, view) for suite, entries in collected.items()},
        )
        for weights in grid(base)
    ]
    points.sort(
        key=lambda point: (
            -point.mean(),
            -point.mean("ndcg_symbol@10"),
            _distance(point.weights, base),
        )
    )
    return points


def _distance(weights: Weights, base: Weights) -> float:
    legs = sum(abs(weights.legs[leg] - base.legs[leg]) for leg in SINGLE_LEGS)
    return (
        legs + abs(weights.multi_hit - base.multi_hit) + abs(weights.layer_boost - base.layer_boost)
    )


def compare(
    collected: Mapping[str, Sequence[Collected]], weights: Weights
) -> dict[str, dict[str, dict[str, float]]]:
    """Each single leg, the full plan and the plan search runs, per suite."""
    views = [*(leg.value for leg in SINGLE_LEGS), "full", "plan"]
    return {
        view: {suite: evaluate(entries, weights, view) for suite, entries in collected.items()}
        for view in views
    }


def render(
    comparison: Mapping[str, Mapping[str, Mapping[str, float]]],
    best: Sequence[Point],
    chosen: Weights,
) -> str:
    """The sweep's report, in Markdown."""
    suites = list(next(iter(comparison.values())))
    lines = [
        "# Hybrid retrieval: legs and weights (2.9)",
        "",
        f"Chosen weights: {describe(chosen)}",
        "",
        f"## Each leg against the fusion ({GATE}, then nDCG@10)",
        "",
        "| retrieval | " + " | ".join(suites) + " |",
        "|---|" + "---|" * len(suites),
    ]
    for view, scores in comparison.items():
        cells = [f"{scores[s][GATE]:.3f} / {scores[s]['ndcg_symbol@10']:.3f}" for s in suites]
        lines.append(f"| {view} | " + " | ".join(cells) + " |")
    lines += [
        "",
        f"## Best grid points (mean {GATE} over the suites)",
        "",
        "| weights | mean | " + " | ".join(suites) + " |",
        "|---|---|" + "---|" * len(suites),
    ]
    for point in best:
        cells = [f"{point.scores[s][GATE]:.3f}" for s in suites]
        lines.append(
            f"| {describe(point.weights)} | {point.mean():.3f} | " + " | ".join(cells) + " |"
        )
    return "\n".join(lines) + "\n"
