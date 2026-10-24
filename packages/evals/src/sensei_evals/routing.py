"""Router eval (2.10): how often the query router picks the class a golden item is labelled
with, rules alone or with the model; a confusion matrix, cost per model call and latency.

Unanswerable items have no router class (the router can't know what a repository lacks), so
they are left out of the accuracy and listed by the class they were routed to.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from sensei_core.usage import CostCalculator
from sensei_engine.llm.anthropic_client import LLMUsage
from sensei_engine.search.route import QueryClass, QueryRouter
from sensei_evals.schema import QueryClass as GoldClass
from sensei_evals.schema import Suite

UNANSWERABLE = GoldClass.UNANSWERABLE.value
CLASSES = [c.value for c in QueryClass]


@dataclass(frozen=True, slots=True)
class LabelledQuery:
    id: str
    query: str
    label: str  # a router class, or "unanswerable"


@dataclass(frozen=True, slots=True)
class Routed:
    query: LabelledQuery
    predicted: str
    source: str
    cost_usd: Decimal
    latency_ms: float


@dataclass(slots=True)
class RoutingReport:
    mode: str  # "rules" or "llm"
    rows: list[Routed] = field(default_factory=list[Routed])

    @property
    def scored(self) -> list[Routed]:
        return [r for r in self.rows if r.query.label != UNANSWERABLE]

    @property
    def accuracy(self) -> float:
        scored = self.scored
        return sum(r.predicted == r.query.label for r in scored) / len(scored) if scored else 0.0

    def confusion(self) -> Counter[tuple[str, str]]:
        return Counter((r.query.label, r.predicted) for r in self.scored)

    def by_source(self) -> Counter[str]:
        return Counter(r.source for r in self.rows)

    def model_calls(self) -> list[Routed]:
        return [r for r in self.rows if r.source == "llm"]

    def cost_per_call(self) -> Decimal:
        calls = self.model_calls()
        return sum((r.cost_usd for r in calls), Decimal(0)) / len(calls) if calls else Decimal(0)

    def latency(self, source: str) -> tuple[float, float] | None:
        """p50 and p95 latency (ms) of the queries ``source`` decided."""
        times = sorted(r.latency_ms for r in self.rows if r.source == source)
        if not times:
            return None
        p95 = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
        return statistics.median(times), p95


def suite_queries(suite: Suite) -> list[LabelledQuery]:
    return [LabelledQuery(item.id, item.query, item.class_.value) for item in suite.items]


def extra_queries(path: Path) -> list[LabelledQuery]:
    """A YAML list of ``{class, query}``: held-out queries for classes the suites lack."""
    rows: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    queries: list[LabelledQuery] = []
    for index, row in enumerate(rows):
        label = str(row["class"])
        if label not in CLASSES:
            msg = f"{path}: item {index} has unknown class {label!r}"
            raise ValueError(msg)
        queries.append(LabelledQuery(f"{path.stem}-{index + 1:02d}", str(row["query"]), label))
    return queries


async def evaluate(
    router: QueryRouter, queries: list[LabelledQuery], calculator: CostCalculator, *, mode: str
) -> RoutingReport:
    report = RoutingReport(mode)
    for query in queries:
        spent: list[LLMUsage] = []

        async def sink(usage: LLMUsage, spent: list[LLMUsage] = spent) -> None:
            spent.append(usage)

        started = time.perf_counter()
        route = await router.route(query.query, scope="evals", on_usage=sink)
        latency_ms = (time.perf_counter() - started) * 1000
        cost = sum(
            (
                calculator.cost(
                    u.provider,
                    u.model,
                    tokens_in=u.input_tokens,
                    tokens_out=u.output_tokens,
                    tokens_cache_read=u.cache_read_tokens,
                    tokens_cache_write=u.cache_write_tokens,
                )
                or Decimal(0)
                for u in spent
            ),
            Decimal(0),
        )
        report.rows.append(Routed(query, route.query_class.value, route.source, cost, latency_ms))
    return report


def render(report: RoutingReport) -> str:
    scored = report.scored
    right = sum(r.predicted == r.query.label for r in scored)
    lines = [
        f"## Router eval ({report.mode})",
        "",
        f"- accuracy: **{report.accuracy:.3f}** ({right}/{len(scored)} answerable queries)",
        f"- decided by: {', '.join(f'{k} {v}' for k, v in sorted(report.by_source().items()))}",
    ]
    calls = report.model_calls()
    if calls:
        lines.append(
            f"- model calls: {len(calls)}, mean cost ${report.cost_per_call():.6f} per call"
        )
    for source in ("rules", "llm", "fallback"):
        latency = report.latency(source)
        if latency is not None:
            lines.append(f"- latency ({source}): p50 {latency[0]:.1f} ms, p95 {latency[1]:.1f} ms")
    confusion = report.confusion()
    labels = [c for c in CLASSES if any(k[0] == c or k[1] == c for k in confusion)]
    lines += ["", "Confusion (rows: label, columns: routed):", ""]
    lines.append("| label \\ routed | " + " | ".join(labels) + " | recall |")
    lines.append("|---" * (len(labels) + 2) + "|")
    for label in labels:
        total = sum(n for (gold, _), n in confusion.items() if gold == label)
        hits = confusion[(label, label)]
        recall = f"{hits / total:.2f}" if total else "-"
        cells = " | ".join(str(confusion[(label, p)] or "") for p in labels)
        lines.append(f"| **{label}** | {cells} | {recall} |")
    unanswerable = Counter(r.predicted for r in report.rows if r.query.label == UNANSWERABLE)
    if unanswerable:
        routed = ", ".join(f"{k} {v}" for k, v in sorted(unanswerable.items()))
        lines += ["", f"Unanswerable items routed to: {routed}"]
    misses = [r for r in scored if r.predicted != r.query.label]
    if misses:
        lines += ["", "Misses:", ""]
        lines += [
            f"- `{r.query.id}` {r.query.label} → {r.predicted} ({r.source}): {r.query.query}"
            for r in misses
        ]
    return "\n".join(lines) + "\n"
