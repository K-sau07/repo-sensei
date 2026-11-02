"""FixBench runs and validations, written as JSON and Markdown (private reports)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from sensei_evals.fixbench.run import BenchRun
from sensei_evals.fixbench.validate import Validation

VERDICTS = ("fixed", "not_reproduced", "blocked", "failed", "unsafe")


def render_run(run: BenchRun) -> str:
    total = len(run.results)
    lines = [
        f"# FixBench run {run.id}",
        "",
        f"Pipeline **{run.pipeline}**, judged {run.runner}; bench v{run.bench_version}; "
        f"repo-sensei {run.git_sha or 'unknown'}{' (uncommitted changes)' if run.dirty else ''}.",
        "",
        f"**{run.count('fixed')} of {total} fixed, {run.unsafe} unsafe.**",
        "",
        "| verdict | bugs |",
        "|---|---|",
        *(f"| {v} | {run.count(v)} |" for v in VERDICTS),
        "",
    ]
    seconds = [r.seconds for r in run.results]
    if seconds:
        lines.append(f"Time per bug: mean {sum(seconds) / len(seconds):.1f}s, "
                     f"max {max(seconds):.1f}s. Cost: ${run.cost_usd:.2f}.")  # fmt: skip
    by_risk = Counter((r.risk, r.verdict) for r in run.results)
    lines += ["", "| risk | fixed | unsafe | stopped |", "|---|---|---|---|"]
    for risk in ("low", "medium", "high"):
        stopped = sum(by_risk[(risk, v)] for v in ("not_reproduced", "blocked", "failed"))
        lines.append(f"| {risk} | {by_risk[(risk, 'fixed')]} | {by_risk[(risk, 'unsafe')]} "
                     f"| {stopped} |")  # fmt: skip
    lines += ["", "| bug | category | risk | verdict | why | files | lines | s |",
              "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for r in run.results:
        why = r.reason.replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(f"| {r.id} | {r.category} | {r.risk} | **{r.verdict}** | {why} "
                     f"| {r.files_changed} | {r.lines_changed} | {r.seconds:.1f} |")  # fmt: skip
    flagged = [r for r in run.results if r.findings]
    if flagged:
        lines += ["", "## Review findings", ""]
        for r in flagged:
            lines += [f"- {r.id}: {finding}" for finding in r.findings]
    return "\n".join(lines) + "\n"


def write_run(run: BenchRun, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"fixbench-{run.id}.json"
    json_path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
    md_path = directory / f"fixbench-{run.id}.md"
    md_path.write_text(render_run(run), encoding="utf-8")
    return json_path, md_path


def render_validation(found: Validation) -> str:
    lines = ["# FixBench validation", ""]
    for layer, passed in found.suites.items():
        lines.append(f"- {layer} suite on the clean app: {'passes' if passed else 'FAILS'}")
        if not passed:
            lines.append(f"  ```\n{found.suite_output.get(layer, '')}\n  ```")
    good = sum(1 for b in found.bugs if b.ok)
    lines += ["", f"**{good} of {len(found.bugs)} bugs valid.**", ""]
    for bug in found.bugs:
        mark = "ok" if bug.ok else "INVALID"
        lines.append(f"- {bug.id}: {mark}")
        lines += [f"  - problem: {p}" for p in bug.problems]
        lines += [f"  - warning: {w}" for w in bug.warnings]
    return "\n".join(lines) + "\n"
