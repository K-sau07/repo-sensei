"""Comparing stored runs: which way each metric is better, the score items are compared on for
each kind of run, and FixBench runs kept as eval runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sensei_evals.fixbench.run import BenchRun, BugResult, Verdict, as_record
from sensei_evals.reports import compare, item_metric, metric_changes
from sensei_evals.runner import RunRecord


def _run(metrics: dict[str, Any], retriever: str = "hybrid", **kw: Any) -> RunRecord:
    return RunRecord(id=uuid.uuid4(), suite="s", suite_version=1, retriever=retriever,
                     config={"ks": [10]}, repo_sha="a" * 40, metrics=metrics, per_item=[],
                     created_at=datetime.now(UTC), git_sha=None, **kw)  # fmt: skip


def test_cost_time_and_errors_are_better_lower() -> None:
    a = {"overall": {"recall_symbol@10": 0.8, "cost_usd": 1.0, "p50_ms": 900.0, "hop_ece": 0.1,
                     "latency": {"p50": 1.0}}}  # fmt: skip
    b = {"overall": {"recall_symbol@10": 0.8, "cost_usd": 2.0, "p50_ms": 600.0, "hop_ece": 0.3,
                     "latency": {"p50": 9.0}}}  # fmt: skip
    regressions, improvements, unchanged = metric_changes(a, b)
    assert [c.name for c in regressions] == ["cost_usd", "hop_ece"]  # the larger loss first
    assert [c.name for c in improvements] == ["p50_ms"]
    assert [c.name for c in unchanged] == ["recall_symbol@10"]  # the breakdown dict left out


def test_items_are_compared_on_the_score_of_their_kind_of_run() -> None:
    assert item_metric(_run({"bench": True})) == "hop_recall"
    assert item_metric(_run({"fixbench": True})) == "fixed"
    assert item_metric(_run({}, retriever="ask:fast")) == "checklist"
    assert item_metric(_run({})) == "recall_symbol@10"


def _bench(*verdicts: Verdict) -> BenchRun:
    results = [
        BugResult(id=f"0{n}-bug", category="logic", risk="low", layer="backend",
                  verdict=verdict, reason="the hidden test still fails" if verdict != "fixed"
                  else "", seconds=2.5, cost_usd=Decimal("0.5"))
        for n, verdict in enumerate(verdicts, 1)
    ]  # fmt: skip
    return BenchRun(id="abc", started_at=datetime.now(UTC), pipeline="sensei", runner="sandbox",
                    bench_version=3, git_sha="f" * 40, dirty=False, results=results)  # fmt: skip


def test_a_fixbench_run_is_kept_as_an_eval_run_one_item_per_bug() -> None:
    record = as_record(_bench("fixed", "unsafe", "failed", "fixed"))
    assert (record.suite, record.suite_version, record.retriever) == ("fixbench", 3, "sensei")
    assert record.metrics["overall"]["fixed"] == 0.5
    assert record.metrics["overall"]["unsafe"] == 0.25
    assert record.metrics["overall"]["cost_usd"] == 2.0
    assert record.metrics["counts"]["failed"] == 1
    first, second = record.per_item[:2]
    assert (first.metrics["fixed"], first.error, first.latency_ms) == (1.0, None, 2500.0)
    assert second.error == "unsafe: the hidden test still fails"
    later = as_record(_bench("fixed", "fixed", "failed", "failed"))
    comparison = compare(record, later)
    assert comparison.metric == "fixed"
    assert [c.name for c in comparison.improvements] == ["unsafe"]  # fewer unsafe: better
    assert [(c.id, c.before, c.after) for c in comparison.items_worse] == [("04-bug", 1.0, 0.0)]
    assert [c.id for c in comparison.items_better] == ["02-bug"]
