"""Run a suite against a retriever and score every item.

Per item, the retriever is asked once for ``max(ks, 10)`` hits; every metric is computed on a
prefix of that one list. Averages skip items where a metric is undefined (no gold), so recall is
over answerable items only, and abstention accuracy is over all of them.
"""

from __future__ import annotations

import math
import statistics
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from uuid_utils.compat import uuid7

from sensei_evals.metrics import (
    Level,
    abstention_accuracy,
    all_layers_hit_at_k,
    covered,
    f_beta,
    mrr,
    ndcg_at_k,
    recall_at_k,
)
from sensei_evals.retrieval import Hit, RepoRef, Retriever
from sensei_evals.schema import GoldenItem, Suite

DEFAULT_KS = (10, 20, 50, 100)
RANK_K = 10  # nDCG and F0.5 look at the first page of results
KEPT_HITS = 10  # hits stored per item, for reading a report


class ItemResult(BaseModel):
    id: str
    query_class: str
    answerable: bool
    query: str
    abstained: bool
    latency_ms: float
    metrics: dict[str, float | None]
    hits: list[dict[str, Any]]  # the first KEPT_HITS: path, lines, score, symbol
    missed: list[dict[str, Any]]  # gold targets not covered at the largest k (symbol level)
    # Answer evals: what was answered, the judge's verdict on each checklist fact and why (for
    # reading its grading against one's own), and why a run that failed did.
    answer: str | None = None
    verdicts: list[dict[str, str]] | None = None
    error: str | None = None
    tools: list[dict[str, Any]] | None = None  # the agent's calls: tool, input, summary, error
    claims: list[dict[str, Any]] | None = None  # each claim: text, its support verdict, its lines
    steps: list[dict[str, Any]] | None = None  # each turn's start (ms) and whether it researched
    streamed: str | None = None  # the answer as written, when verification rewrote it


class RunRecord(BaseModel):
    """A finished run: what the JSON report holds and what ``eval_runs`` stores."""

    id: uuid.UUID
    suite: str
    suite_version: int
    retriever: str
    config: dict[str, Any]
    repo_sha: str
    metrics: dict[str, Any]
    per_item: list[ItemResult]
    created_at: datetime
    git_sha: str | None


def _hit_json(hit: Hit) -> dict[str, Any]:
    return {
        "path": hit.path,
        "lines": list(hit.lines) if hit.lines else None,
        "score": hit.score,
        "symbol": hit.symbol,
    }


def score_item(item: GoldenItem, hits: Sequence[Hit], ks: Sequence[int]) -> dict[str, float | None]:
    gold = item.gold
    scores: dict[str, float | None] = {}
    for k in ks:
        scores[f"recall_file@{k}"] = recall_at_k(hits, gold, k, Level.FILE)
        scores[f"recall_symbol@{k}"] = recall_at_k(hits, gold, k, Level.SYMBOL)
        scores[f"all_layers@{k}"] = all_layers_hit_at_k(hits, gold, k, Level.SYMBOL)
    scores[f"ndcg_file@{RANK_K}"] = ndcg_at_k(hits, gold, RANK_K, Level.FILE)
    scores[f"ndcg_symbol@{RANK_K}"] = ndcg_at_k(hits, gold, RANK_K, Level.SYMBOL)
    # At a fixed depth, so runs asked for different ks still compare.
    scores[f"mrr_file@{RANK_K}"] = mrr(hits, gold, Level.FILE, RANK_K)
    scores[f"mrr_symbol@{RANK_K}"] = mrr(hits, gold, Level.SYMBOL, RANK_K)
    scores[f"f0.5_file@{RANK_K}"] = f_beta(hits[:RANK_K], gold, 0.5, "file")
    scores[f"f0.5_line@{RANK_K}"] = f_beta(hits[:RANK_K], gold, 0.5, "line")
    return scores


def _mean(values: Sequence[float | None]) -> float | None:
    defined = [v for v in values if v is not None]
    return statistics.fmean(defined) if defined else None


def _averages(results: Sequence[ItemResult]) -> dict[str, float | None]:
    keys = list(dict.fromkeys(key for result in results for key in result.metrics))
    averages = {key: _mean([result.metrics.get(key) for result in results]) for key in keys}
    averages["abstention_accuracy"] = abstention_accuracy(
        (result.answerable, result.abstained) for result in results
    )
    return averages


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def aggregate(results: Sequence[ItemResult]) -> dict[str, Any]:
    by_class: dict[str, list[ItemResult]] = defaultdict(list)
    for result in results:
        by_class[result.query_class].append(result)
    latencies = [result.latency_ms for result in results]
    return {
        "overall": _averages(results),
        "by_class": {name: _averages(group) for name, group in sorted(by_class.items())},
        "counts": {
            "items": len(results),
            "answerable": sum(result.answerable for result in results),
            "by_class": {name: len(group) for name, group in sorted(by_class.items())},
        },
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
    }


async def run_suite(
    suite: Suite,
    items: Sequence[GoldenItem],
    retriever: Retriever,
    repo: RepoRef,
    *,
    ks: Sequence[int] = DEFAULT_KS,
    config: dict[str, Any] | None = None,
    git_sha: str | None = None,
) -> RunRecord:
    """Search for every item (in order, one at a time, so latencies are comparable)."""
    ks = sorted(set(ks))
    if not ks:
        msg = "give at least one k"
        raise ValueError(msg)
    depth = max([*ks, RANK_K])
    results: list[ItemResult] = []
    notes: dict[str, Any] = {}
    try:
        prepare = getattr(retriever, "prepare", None)
        if prepare is not None:
            await prepare(repo)  # an index to build: not part of any search's latency
        for item in items:
            started = time.perf_counter()
            hits = await retriever.search(item.query, repo, depth)
            latency_ms = (time.perf_counter() - started) * 1000
            found = covered(hits[: ks[-1]], item.gold, Level.SYMBOL)
            missed = [target for target in item.gold if target not in found]
            results.append(
                ItemResult(
                    id=item.id,
                    query_class=item.class_.value,
                    answerable=item.answerable,
                    query=item.query,
                    abstained=not hits,
                    latency_ms=round(latency_ms, 3),
                    metrics=score_item(item, hits, ks),
                    hits=[_hit_json(hit) for hit in hits[:KEPT_HITS]],
                    missed=[t.model_dump(mode="json", exclude_none=True) for t in missed],
                )
            )
        report = getattr(retriever, "report", None)
        if report is not None:
            notes["retriever_report"] = dict(report())
    finally:
        close = getattr(retriever, "aclose", None)
        if close is not None:
            await close()
    return RunRecord(
        id=uuid7(),
        suite=suite.name,
        suite_version=suite.version,
        retriever=retriever.name,
        config={"repo": repo.id, "ks": ks, "depth": depth, **(config or {}), **notes},
        repo_sha=repo.sha,
        metrics=aggregate(results),
        per_item=results,
        created_at=datetime.now(UTC),
        git_sha=git_sha,
    )
