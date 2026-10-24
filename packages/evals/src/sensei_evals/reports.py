"""Run reports: JSON (the full record) and markdown (for reading), plus run-to-run comparisons."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sensei_evals.runner import RANK_K, ItemResult, RunRecord

EPSILON = 1e-9
SUMMARY_METRICS = (
    "recall_file@{k}",
    "recall_symbol@{k}",
    "all_layers@{k}",
    f"ndcg_symbol@{RANK_K}",
    f"mrr_symbol@{RANK_K}",
)


def write_run(run: RunRecord, directory: Path) -> tuple[Path, Path]:
    """Write ``<id>.json`` and ``<id>.md`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{run.id}.json"
    json_path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
    md_path = directory / f"{run.id}.md"
    md_path.write_text(render_run(run), encoding="utf-8")
    return json_path, md_path


def read_run(path: Path) -> RunRecord:
    return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _headline_k(run: RunRecord) -> int:
    ks: list[int] = run.config.get("ks") or [RANK_K]
    return RANK_K if RANK_K in ks else min(ks)


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(_fmt(cell) for cell in row) + " |" for row in rows)
    return lines


def _missed_items(per_item: Sequence[ItemResult]) -> list[ItemResult]:
    return [item for item in per_item if item.answerable and item.missed]


def render_run(run: RunRecord) -> str:
    k = _headline_k(run)
    overall: dict[str, Any] = run.metrics.get("overall", {})
    counts: dict[str, Any] = run.metrics.get("counts", {})
    latency: dict[str, Any] = run.metrics.get("latency_ms", {})
    lines = [
        f"# Eval run `{run.id}`",
        "",
        f"- **Suite:** {run.suite} v{run.suite_version} ({counts.get('items', 0)} items, "
        f"{counts.get('answerable', 0)} answerable)",
        f"- **Retriever:** {run.retriever}",
        f"- **Repository:** {run.config.get('repo')} @ `{run.repo_sha}`",
        f"- **repo-sensei:** `{run.git_sha or 'unknown'}`"
        + (" (uncommitted changes)" if run.config.get("dirty") else ""),
        f"- **Created:** {run.created_at.isoformat()}",
        f"- **Latency:** p50 {_fmt(latency.get('p50'))} ms, p95 {_fmt(latency.get('p95'))} ms",
        "",
        "## Overall",
        "",
        *_table(["metric", "value"], list(overall.items())),
        "",
        "## By class",
        "",
    ]
    columns = [name.format(k=k) for name in SUMMARY_METRICS]
    by_class: dict[str, dict[str, Any]] = run.metrics.get("by_class", {})
    class_counts: dict[str, int] = counts.get("by_class", {})
    rows = [
        [
            name,
            class_counts.get(name, 0),
            *(scores.get(c) for c in columns),
            scores.get("abstention_accuracy"),
        ]
        for name, scores in by_class.items()
    ]
    lines += _table(["class", "items", *columns, "abstention"], rows)
    missed = _missed_items(run.per_item)
    if missed:
        lines += ["", "## Gold not found", ""]
        for item in missed:
            lines.append(f"- **{item.id}** ({item.query_class}): {item.query}")
            lines.extend(
                f"  - {target['layer']}: `{target['path']}`"
                + (f" {target['symbol']}" if target.get("symbol") else "")
                + (f" L{target['lines'][0]}-{target['lines'][1]}" if target.get("lines") else "")
                for target in item.missed
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- comparison


@dataclass(frozen=True, slots=True)
class MetricChange:
    name: str
    before: float | None
    after: float | None

    @property
    def delta(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class ItemChange:
    id: str
    query: str
    before: float | None
    after: float | None


@dataclass(frozen=True, slots=True)
class Comparison:
    a: RunRecord
    b: RunRecord
    metric: str  # the per-item metric items are compared on
    regressions: list[MetricChange]
    improvements: list[MetricChange]
    unchanged: list[MetricChange]
    items_worse: list[ItemChange]
    items_better: list[ItemChange]
    warnings: list[str]


def compare(a: RunRecord, b: RunRecord) -> Comparison:
    """What changed from run ``a`` to run ``b``; regressions sorted worst first."""
    before: dict[str, Any] = a.metrics.get("overall", {})
    after: dict[str, Any] = b.metrics.get("overall", {})
    changes = [
        MetricChange(name, before.get(name), after.get(name))
        for name in dict.fromkeys([*before, *after])
    ]
    regressions = sorted(
        (c for c in changes if c.delta is not None and c.delta < -EPSILON),
        key=lambda c: c.delta or 0.0,
    )
    improvements = sorted(
        (c for c in changes if c.delta is not None and c.delta > EPSILON),
        key=lambda c: -(c.delta or 0.0),
    )
    unchanged = [c for c in changes if c not in regressions and c not in improvements]

    k = min(_headline_k(a), _headline_k(b))
    metric = f"recall_symbol@{k}"
    items_a = {item.id: item for item in a.per_item}
    worse: list[ItemChange] = []
    better: list[ItemChange] = []
    for item in b.per_item:
        old = items_a.get(item.id)
        if old is None:
            continue
        x, y = old.metrics.get(metric), item.metrics.get(metric)
        if x is None or y is None or abs(y - x) <= EPSILON:
            continue
        (worse if y < x else better).append(ItemChange(item.id, item.query, x, y))
    worse.sort(key=lambda c: (c.after or 0.0) - (c.before or 0.0))
    better.sort(key=lambda c: (c.before or 0.0) - (c.after or 0.0))

    warnings: list[str] = []
    if (a.suite, a.suite_version) != (b.suite, b.suite_version):
        warnings.append(
            f"different suites: {a.suite} v{a.suite_version} vs {b.suite} v{b.suite_version}"
        )
    if a.repo_sha != b.repo_sha:
        warnings.append(f"different repository commits: {a.repo_sha} vs {b.repo_sha}")
    file_a, file_b = a.config.get("suite_sha256"), b.config.get("suite_sha256")
    if file_a and file_b and file_a != file_b:
        warnings.append("the suite file changed between the runs (same name and version)")
    if a.config.get("ks") != b.config.get("ks"):
        warnings.append(f"different ks: {a.config.get('ks')} vs {b.config.get('ks')}")
    if only := sorted(set(items_a) ^ {item.id for item in b.per_item}):
        warnings.append(f"{len(only)} items are in only one run: {', '.join(only[:10])}")
    return Comparison(a, b, metric, regressions, improvements, unchanged, worse, better, warnings)


def render_comparison(comparison: Comparison) -> str:
    a, b = comparison.a, comparison.b
    lines = [
        f"# Compare `{a.id}` → `{b.id}`",
        "",
        f"- **A:** {a.retriever} on {a.suite} v{a.suite_version} ({a.created_at.isoformat()})",
        f"- **B:** {b.retriever} on {b.suite} v{b.suite_version} ({b.created_at.isoformat()})",
    ]
    lines += [f"- ⚠ {warning}" for warning in comparison.warnings]
    for title, changes in (
        ("Regressions", comparison.regressions),
        ("Improvements", comparison.improvements),
        ("Unchanged", comparison.unchanged),
    ):
        if not changes:
            continue
        lines += ["", f"## {title}", ""]
        lines += _table(
            ["metric", "A", "B", "Δ"], [(c.name, c.before, c.after, c.delta) for c in changes]
        )
    for title, items in (
        (f"Items that got worse ({comparison.metric})", comparison.items_worse),
        (f"Items that got better ({comparison.metric})", comparison.items_better),
    ):
        if not items:
            continue
        lines += ["", f"## {title}", ""]
        lines += _table(
            ["item", "A", "B", "query"], [(c.id, c.before, c.after, c.query) for c in items]
        )
    return "\n".join(lines) + "\n"
