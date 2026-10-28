"""CrossLayerBench (3.13, engine §18.5): does a trace from a UI entry reach the code that serves it,
hop by hop, across the client, the API, the handler and the database?

**A bench** is a YAML file (``evals/private/crosslayerbench/<version>.yaml``, private)::

    name: crosslayerbench
    version: v0
    flows:
      - id: ft-add-item
        repo: fastapi-template
        sha: b1125d8839482cb7bbae603ddc6d0081bf87532e
        title: adding an item from the dialog to the stored row
        hops:
          - {layer: ui, path: frontend/src/components/Items/AddItem.tsx, symbol: AddItem,
             lines: [40, 141]}
          - {layer: client, path: frontend/src/client/sdk.gen.ts,
             symbol: ItemsService.createItem, lines: [307, 318]}
          - {layer: handler, path: backend/app/api/routes/items.py, symbol: create_item,
             lines: [61, 72]}
          - {layer: db, path: backend/app/models.py, symbol: Item, lines: [87, 93]}

A flow's hops are the gold path in order, each the code (file, symbol, lines) a correct trace
passes through; the first is the **entry** the trace starts from. Every hop is checked against
the pinned checkout as a golden suite's targets are (:func:`as_suite`).

**A tracer** (:class:`Tracer`) returns up to 3 paths for a flow's entry, best first, each a list
of :class:`TraceHop` (what node, where, and the confidence of the edge into it) and its
confidence (the product of its edges').

**Scoring** (:func:`score_flow`) matches each hop of a path to the first gold hop whose span it
overlaps (same file, overlapping lines; a table hop by the model class it maps). A hop with no
place (a node in no file) is neither right nor wrong. Per flow, on the best path:

- ``hop_recall``: the gold hops some trace hop matched;
- ``hop_precision``: the placed trace hops that matched a gold hop;
- ``exact``: the trace's matches, consecutive repeats collapsed, are the gold hops in order, with
  no unmatched placed hop (``exact_at_3``: any of the 3 paths is);
- ``layers_hit``: every layer of the gold has a matched hop.

Over a run (:func:`aggregate`): the means, and **calibration**: the expected calibration error
and Brier score of the best path's confidence against ``exact``, and of each hop's edge
confidence against the hop being right.

**A run** (:func:`run_bench`, ``sensei-eval run --suite crosslayerbench-v0 --repo <id>
--retriever <tracer>``) covers one repository at its pinned commit and is a
:class:`~sensei_evals.runner.RunRecord` like a suite's: written to the reports folder, stored in
``eval_runs``, and read by ``report`` and ``compare`` (flows compared on ``hop_recall``).
**Renamed copies** made by ``sensei-eval rename`` from a bench repository get its flows
(:func:`renamed_flows`), derived from the copy's mapping each time, so never stale.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from uuid_utils.compat import uuid7

from sensei_evals.rename import (
    RenameResult,
    TranslateError,
    language_of,
    translate_query,
    translate_target,
)
from sensei_evals.repos import load_renamed, mapping_path, read_mapping
from sensei_evals.runner import ItemResult, RunRecord, percentile
from sensei_evals.schema import (
    ITEM_ID,
    SHA,
    GoldenItem,
    GoldTarget,
    Layer,
    QueryClass,
    Suite,
    parse_yaml,
)

BENCH = "crosslayerbench"
MAX_PATHS = 3
CALIBRATION_BINS = 10


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Flow(_Strict):
    id: str
    repo: str
    sha: str
    title: Annotated[str, Field(min_length=3)]
    hops: list[GoldTarget]
    notes: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ITEM_ID.fullmatch(value):
            msg = "must be lowercase letters, digits, '.', '_' or '-'"
            raise ValueError(msg)
        return value

    @field_validator("sha")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not SHA.fullmatch(value):
            msg = "must be a full 40-character lowercase commit sha"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _path(self) -> Self:
        if len(self.hops) < 2:
            msg = "a flow needs at least two hops: its entry and where it goes"
            raise ValueError(msg)
        if any(hop.symbol is None for hop in self.hops):
            msg = "every hop names its symbol (and so its lines)"
            raise ValueError(msg)
        if len({(hop.path, hop.lines) for hop in self.hops}) != len(self.hops):
            msg = "a hop repeats"
            raise ValueError(msg)
        return self

    @property
    def entry(self) -> GoldTarget:
        return self.hops[0]

    @property
    def layers(self) -> list[Layer]:
        seen: list[Layer] = []
        for hop in self.hops:
            if hop.layer not in seen:
                seen.append(hop.layer)
        return seen


class Bench(_Strict):
    name: str
    version: Annotated[str, Field(pattern=r"^v\d+$")]  # v0, v1, ...
    flows: list[Flow]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [flow.id for flow in self.flows]
        if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
            msg = f"flow ids repeat: {', '.join(duplicates)}"
            raise ValueError(msg)
        return self

    @property
    def number(self) -> int:
        return int(self.version.removeprefix("v"))

    @property
    def repos(self) -> list[str]:
        return sorted({flow.repo for flow in self.flows})

    def for_repo(self, repo: str) -> list[Flow]:
        return [flow for flow in self.flows if flow.repo == repo]


def parse_bench(text: str, source: str = "<bench>") -> Bench:
    """A bench from YAML; :class:`~sensei_evals.schema.SuiteError` lists every problem with its
    line."""
    return parse_yaml(text, source, Bench)


def load_bench(path: Path) -> Bench:
    return parse_bench(path.read_text(encoding="utf-8"), str(path))


def bench_path(root: Path, name: str) -> Path | None:
    """``crosslayerbench-<version>`` as ``evals/private/crosslayerbench/<version>.yaml`` (a path
    to such a file as itself); None if ``name`` isn't a bench."""
    candidate = Path(name)
    if candidate.suffix in (".yaml", ".yml") and candidate.parent.name == BENCH:
        return candidate
    prefix = f"{BENCH}-"
    if not name.startswith(prefix):
        return None
    return root / "evals" / "private" / BENCH / f"{name.removeprefix(prefix)}.yaml"


def as_suite(bench: Bench) -> Suite:
    """The bench as a golden suite of flow items, one per flow, its hops the gold: what the
    suite checks (pinned commits, files, symbols and their lines) checks every hop."""
    items = [
        GoldenItem.model_validate(
            {
                "id": flow.id,
                "repo": flow.repo,
                "sha": flow.sha,
                "query": flow.title,
                "class": QueryClass.FLOW,
                "layers": flow.layers,
                "gold": flow.hops,
                "notes": flow.notes,
            }
        )
        for flow in bench.flows
    ]
    return Suite(name=f"{bench.name}-{bench.version}", version=bench.number, items=items)


# ---------------------------------------------------------------------- traces


@dataclass(frozen=True, slots=True)
class TraceHop:
    kind: str  # the node's: component, function, http_client_call, api_endpoint, table, ...
    name: str
    path: str | None = None  # None: a node in no file (a table)
    lines: tuple[int, int] | None = None
    confidence: float | None = None  # the edge into it (None: the entry)
    layer: str | None = None  # what the tracer says it is (reported, not scored)
    model: tuple[str, tuple[int, int]] | None = None  # a table's ORM class: its file and lines

    @property
    def placed(self) -> bool:
        return (self.path is not None and self.lines is not None) or self.model is not None


@dataclass(frozen=True, slots=True)
class TracePath:
    hops: tuple[TraceHop, ...]
    confidence: float  # the product of its edges'
    breaks: tuple[str, ...] = ()  # where it stops short, and why


class Tracer(Protocol):
    name: str

    def trace(self, flow: Flow) -> Sequence[TracePath]:
        """Up to :data:`MAX_PATHS` paths from ``flow``'s entry, best first."""
        ...


# ---------------------------------------------------------------------- scoring


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def match(hop: TraceHop, gold: Sequence[GoldTarget]) -> int | None:
    """The first gold hop ``hop`` lands on (None: none, or it has no place)."""
    places: list[tuple[str, tuple[int, int]]] = []
    if hop.path is not None and hop.lines is not None:
        places.append((hop.path, hop.lines))
    if hop.model is not None:
        places.append(hop.model)
    for index, target in enumerate(gold):
        if target.lines is None:
            continue
        if any(path == target.path and _overlaps(lines, target.lines) for path, lines in places):
            return index
    return None


@dataclass(frozen=True, slots=True)
class PathScore:
    hop_recall: float
    hop_precision: float | None  # None: no placed hop
    exact: bool
    layers_hit: bool
    matched: tuple[int | None, ...]  # each trace hop's gold hop


def score_path(path: TracePath, gold: Sequence[GoldTarget]) -> PathScore:
    matched = tuple(match(hop, gold) for hop in path.hops)
    placed = [m for hop, m in zip(path.hops, matched, strict=True) if hop.placed]
    found = {m for m in matched if m is not None}
    collapsed: list[int] = []
    for m in matched:
        if m is not None and (not collapsed or collapsed[-1] != m):
            collapsed.append(m)
    exact = collapsed == list(range(len(gold))) and all(m is not None for m in placed)
    layers = {gold[m].layer for m in found}
    return PathScore(
        hop_recall=len(found) / len(gold),
        hop_precision=sum(m is not None for m in placed) / len(placed) if placed else None,
        exact=exact,
        layers_hit=all(target.layer in layers for target in gold),
        matched=matched,
    )


class FlowResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    repo: str
    hops: int  # gold hops
    paths: int  # paths the tracer returned
    hop_recall: float
    hop_precision: float | None
    exact: bool
    exact_at_3: bool
    layers_hit: bool
    confidence: float | None  # the best path's
    hop_confidences: list[tuple[float, bool]] = Field(default_factory=list[tuple[float, bool]])
    breaks: list[str] = Field(default_factory=list[str])
    trace: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])  # the best path
    error: str | None = None
    seconds: float = 0.0


def _hop_json(hop: TraceHop, matched: int | None) -> dict[str, Any]:
    return {
        "kind": hop.kind,
        "name": hop.name,
        "path": hop.path,
        "lines": list(hop.lines) if hop.lines else None,
        "confidence": hop.confidence,
        "layer": hop.layer,
        "gold": matched,
    }


def score_flow(flow: Flow, paths: Sequence[TracePath], seconds: float = 0.0) -> FlowResult:
    """A flow's result for the paths a tracer returned (see the module)."""
    paths = list(paths)[:MAX_PATHS]
    if not paths:
        return FlowResult(
            id=flow.id, repo=flow.repo, hops=len(flow.hops), paths=0, hop_recall=0.0,
            hop_precision=None, exact=False, exact_at_3=False, layers_hit=False, confidence=None,
            seconds=seconds,
        )  # fmt: skip
    scores = [score_path(path, flow.hops) for path in paths]
    best, best_score = paths[0], scores[0]
    hop_confidences = [
        (hop.confidence, m is not None)
        for hop, m in zip(best.hops, best_score.matched, strict=True)
        if hop.confidence is not None and hop.placed
    ]
    return FlowResult(
        id=flow.id,
        repo=flow.repo,
        hops=len(flow.hops),
        paths=len(paths),
        hop_recall=best_score.hop_recall,
        hop_precision=best_score.hop_precision,
        exact=best_score.exact,
        exact_at_3=any(score.exact for score in scores),
        layers_hit=best_score.layers_hit,
        confidence=best.confidence,
        hop_confidences=hop_confidences,
        breaks=list(best.breaks),
        trace=[_hop_json(h, m) for h, m in zip(best.hops, best_score.matched, strict=True)],
        seconds=seconds,
    )


def calibration(
    pairs: Iterable[tuple[float, bool]], bins: int = CALIBRATION_BINS
) -> dict[str, Any]:
    """Expected calibration error (equal-width bins, weighted by size) and Brier score of
    confidences against outcomes; None for both with nothing to judge."""
    pairs = [(min(max(c, 0.0), 1.0), ok) for c, ok in pairs]
    if not pairs:
        return {"n": 0, "ece": None, "brier": None, "bins": []}
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, ok in pairs:
        buckets[min(int(confidence * bins), bins - 1)].append((confidence, ok))
    ece = 0.0
    table: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        mean = sum(c for c, _ in bucket) / len(bucket)
        accuracy = sum(ok for _, ok in bucket) / len(bucket)
        ece += len(bucket) / len(pairs) * abs(mean - accuracy)
        table.append({
            "bin": [index / bins, (index + 1) / bins], "n": len(bucket),
            "confidence": round(mean, 4), "accuracy": round(accuracy, 4),
        })  # fmt: skip
    brier = sum((c - ok) ** 2 for c, ok in pairs) / len(pairs)
    return {"n": len(pairs), "ece": round(ece, 4), "brier": round(brier, 4), "bins": table}


def _mean(values: Iterable[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return round(sum(kept) / len(kept), 4) if kept else None


def _means(group: Sequence[FlowResult]) -> dict[str, float | None]:
    return {
        "hop_recall": _mean(r.hop_recall for r in group),
        "hop_precision": _mean(r.hop_precision for r in group),
        "exact": _mean(float(r.exact) for r in group),
        "exact_at_3": _mean(float(r.exact_at_3) for r in group),
        "layers_hit": _mean(float(r.layers_hit) for r in group),
        "traced": _mean(float(r.paths > 0) for r in group),
    }


def aggregate(results: Sequence[FlowResult]) -> dict[str, Any]:
    """A run's metrics: the means (overall, with the calibration errors, and per repository),
    the calibration tables, each flow's breaks and errors (see the module)."""
    paths = calibration((r.confidence, r.exact) for r in results if r.confidence is not None)
    hops = calibration(pair for r in results for pair in r.hop_confidences)
    latencies = [round(r.seconds * 1000, 3) for r in results]
    return {
        "bench": BENCH,
        "overall": {
            **_means(results),
            "path_ece": paths["ece"],
            "path_brier": paths["brier"],
            "hop_ece": hops["ece"],
            "hop_brier": hops["brier"],
        },
        "by_repo": {
            repo: {"flows": len(group), **_means(group)}
            for repo in sorted({r.repo for r in results})
            if (group := [r for r in results if r.repo == repo])
        },
        "calibration": {"paths": paths, "hops": hops},
        "counts": {"items": len(results), "answerable": len(results)},
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
        "breaks": {r.id: r.breaks for r in results if r.breaks},
        "errors": {r.id: r.error for r in results if r.error is not None},
    }


def _item(flow: Flow, result: FlowResult) -> ItemResult:
    matched = {hop["gold"] for hop in result.trace}
    return ItemResult(
        id=result.id,
        query_class=QueryClass.FLOW.value,
        answerable=True,
        query=flow.title,
        abstained=result.paths == 0,
        latency_ms=round(result.seconds * 1000, 3),
        metrics={
            "hop_recall": result.hop_recall,
            "hop_precision": result.hop_precision,
            "exact": float(result.exact),
            "exact_at_3": float(result.exact_at_3),
            "layers_hit": float(result.layers_hit),
            "confidence": result.confidence,
        },
        hits=result.trace,
        missed=[
            hop.model_dump(mode="json", exclude_none=True)
            for index, hop in enumerate(flow.hops)
            if index not in matched
        ],
    )


def run_bench(
    bench: Bench,
    tracer: Tracer,
    flows: Sequence[Flow],
    *,
    repo_sha: str,
    config: Mapping[str, Any] | None = None,
    git_sha: str | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> RunRecord:
    """Trace each of ``flows`` (one repository's, at ``repo_sha``) and score it, as a run like
    a suite's (stored, compared and reported the same way); a tracer's error fails that flow
    only, recorded with it."""
    results: list[FlowResult] = []
    for flow in flows:
        started = clock()
        try:
            paths = tracer.trace(flow)
        except Exception as error:  # one flow's failure is its result
            result = score_flow(flow, [], clock() - started)
            results.append(result.model_copy(update={"error": f"{type(error).__name__}: {error}"}))
            continue
        results.append(score_flow(flow, paths, clock() - started))
    return RunRecord(
        id=uuid7(),
        suite=f"{bench.name}-{bench.version}",
        suite_version=bench.number,
        retriever=tracer.name,
        config={"repo": flows[0].repo if flows else None, **(config or {})},
        repo_sha=repo_sha,
        metrics=aggregate(results),
        per_item=[_item(flow, result) for flow, result in zip(flows, results, strict=True)],
        created_at=datetime.now(UTC),
        git_sha=git_sha,
    )


# ---------------------------------------------------------------------- renamed copies


def renamed_flows(bench: Bench, checkouts: Path) -> tuple[list[Flow], list[str]]:
    """The flows of each renamed copy in ``checkouts`` made from a repository of the bench, at
    the flows' commit: ``<id>.renamed``, on the copy, with paths and symbols (and what the
    title quotes in backticks) renamed by the copy's mapping, lines unchanged. Derived each
    time, so never stale. Also why a copy's flows couldn't be made."""
    flows: list[Flow] = []
    problems: list[str] = []
    for copy, pin in sorted(load_renamed(checkouts).items()):
        mapping = read_mapping(mapping_path(checkouts, copy))
        if mapping is None:
            continue
        result = RenameResult.from_mapping(mapping)
        source = bench.for_repo(result.source.get("repo", ""))
        if not source:
            continue
        stale = sorted({f.sha for f in source} - {result.source.get("sha", "")})
        if stale:
            problems.append(
                f"{copy} was made from {result.source.get('repo')} at "
                f"{result.source.get('sha')}, its flows are pinned to {', '.join(stale)}: "
                "re-run rename"
            )
            continue
        for flow in source:
            try:
                hops = [translate_target(hop, result) for hop in flow.hops]
            except TranslateError as error:
                problems.append(f"flow {flow.id}: {error}")
                continue
            languages = frozenset(
                language for hop in flow.hops if (language := language_of(hop.path)) is not None
            )
            title = translate_query(flow.title, QueryClass.FLOW, result.plan, languages or None)
            flows.append(
                flow.model_copy(
                    update={
                        "id": f"{flow.id}.renamed",
                        "repo": copy,
                        "sha": pin.sha,
                        "title": title,
                        "hops": hops,
                    }
                )
            )
    return flows, problems


def with_renamed(bench: Bench, checkouts: Path) -> tuple[Bench, list[str]]:
    """The bench and its renamed copies' flows (:func:`renamed_flows`)."""
    flows, problems = renamed_flows(bench, checkouts)
    return bench.model_copy(update={"flows": [*bench.flows, *flows]}), problems


# ---------------------------------------------------------------------- tracers


@dataclass(frozen=True, slots=True)
class TracerContext:
    repo: str
    sha: str
    checkout: Path | None
    options: Mapping[str, str] = field(default_factory=dict[str, str])


def _lines(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(cast("list[Any]", value)) == 2:
        start, end = cast("list[Any]", value)
        if isinstance(start, int) and isinstance(end, int):
            return start, end
    return None


def _trace_hop(hop: Mapping[str, Any]) -> TraceHop:
    node = cast("dict[str, Any]", hop.get("node") or {})
    edge = cast("dict[str, Any] | None", hop.get("edge"))
    model = cast("dict[str, Any] | None", node.get("model"))
    model_lines = _lines(model.get("lines")) if model else None
    return TraceHop(
        kind=str(node.get("kind", "")),
        name=str(node.get("name", "")),
        path=node.get("path"),
        lines=_lines(node.get("lines")),
        confidence=float(edge["confidence"]) if edge and "confidence" in edge else None,
        layer=hop.get("layer"),
        model=(str(model["path"]), model_lines)
        if model and model.get("path") and model_lines
        else None,
    )


def paths_from_trace(answer: Mapping[str, Any]) -> list[TracePath]:
    """3.12's ``trace_feature`` answer as paths: ``{paths: [{hops: [{layer, node: {kind, name,
    symbol_key, path, lines, model?: {symbol_key, path, lines}}, edge: {confidence, ...} |
    null}], path_confidence, breaks: [{after_hop, reason}]}]}``."""
    found: list[TracePath] = []
    for raw in cast("list[dict[str, Any]]", answer.get("paths") or [])[:MAX_PATHS]:
        hops = tuple(_trace_hop(hop) for hop in cast("list[dict[str, Any]]", raw.get("hops") or []))
        breaks = tuple(
            f"after hop {b.get('after_hop')}: {b.get('reason')}"
            for b in cast("list[dict[str, Any]]", raw.get("breaks") or [])
        )
        found.append(TracePath(hops, float(raw.get("path_confidence", 0.0)), breaks))
    return found


class GoldTracer:
    """Returns each flow's gold path: what a perfect tracer scores (1.0 everywhere), a check of
    the harness itself."""

    name = "gold"

    def trace(self, flow: Flow) -> Sequence[TracePath]:
        hops = tuple(
            TraceHop(
                kind="gold",
                name=hop.symbol or hop.path,
                path=hop.path,
                lines=hop.lines,
                confidence=None if index == 0 else 1.0,
                layer=hop.layer.value,
            )
            for index, hop in enumerate(flow.hops)
        )
        return [TracePath(hops, 1.0)]


class AnswersTracer:
    """Scores answers ``trace_feature`` already gave: ``--opt answers=<file.json>``, a JSON
    object of flow id to its answer (:func:`paths_from_trace`); a flow without one is untraced."""

    name = "answers"

    def __init__(self, answers: Mapping[str, Mapping[str, Any]]) -> None:
        self.answers = answers

    @classmethod
    def build(cls, context: TracerContext) -> AnswersTracer:
        file = context.options.get("answers")
        if not file:
            msg = "the answers tracer needs --opt answers=<file.json>"
            raise ValueError(msg)
        try:
            data = json.loads(Path(file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            msg = f"can't read answers from {file}: {error}"
            raise ValueError(msg) from None
        if not isinstance(data, dict):
            msg = f"{file} must hold a JSON object of flow id to answer"
            raise ValueError(msg)
        return cls(cast("dict[str, dict[str, Any]]", data))

    def trace(self, flow: Flow) -> Sequence[TracePath]:
        answer = self.answers.get(flow.id)
        return paths_from_trace(answer) if answer is not None else []


TracerFactory = Callable[[TracerContext], Tracer]
TRACERS: dict[str, TracerFactory] = {
    "gold": lambda _context: GoldTracer(),
    "answers": AnswersTracer.build,
}


# ---------------------------------------------------------------------- reports


def _num(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _yes(value: Any) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


def render_run(run: RunRecord) -> str:
    """A bench run as Markdown: the means, per repository, calibration, and every flow."""
    m = run.metrics
    overall = cast("dict[str, Any]", m.get("overall", {}))
    header = "| | flows | hop recall | hop precision | exact | exact@3 | layers hit | traced |"
    lines = [
        f"# {run.suite}: `{run.retriever}` (run `{run.id}`)",
        "",
        f"- **Repository:** {run.config.get('repo')} @ `{run.repo_sha}`",
        f"- **repo-sensei:** `{run.git_sha or 'unknown'}`"
        + (" (uncommitted changes)" if run.config.get("dirty") else ""),
        f"- **Created:** {run.created_at.isoformat()}",
        "",
        header,
        "|---|---|---|---|---|---|---|---|",
    ]
    counts = cast("dict[str, Any]", m.get("counts", {}))
    groups = {"**all**": (overall, counts.get("items", 0))}
    by_repo = cast("dict[str, dict[str, Any]]", m.get("by_repo", {}))
    if len(by_repo) > 1:
        groups |= {repo: (means, means.get("flows")) for repo, means in by_repo.items()}
    for label, (means, n) in groups.items():
        lines.append(
            f"| {label} | {n if n is not None else ''} | {_num(means.get('hop_recall'))} "
            f"| {_num(means.get('hop_precision'))} | {_num(means.get('exact'))} "
            f"| {_num(means.get('exact_at_3'))} | {_num(means.get('layers_hit'))} "
            f"| {_num(means.get('traced'))} |"
        )
    calibrations = cast("dict[str, dict[str, Any]]", m.get("calibration", {}))
    paths, hops = calibrations.get("paths", {}), calibrations.get("hops", {})
    breaks = cast("dict[str, list[str]]", m.get("breaks", {}))
    errors = cast("dict[str, str]", m.get("errors", {}))
    lines += [
        "",
        f"**Calibration.** Paths (confidence against exact): ECE {_num(paths.get('ece'))}, "
        f"Brier {_num(paths.get('brier'))} over {paths.get('n', 0)}. Hops (edge confidence "
        f"against right): ECE {_num(hops.get('ece'))}, Brier {_num(hops.get('brier'))} over "
        f"{hops.get('n', 0)}.",
        "",
        "| flow | hops | recall | precision | exact | exact@3 | layers | confidence | breaks |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in run.per_item:
        s = item.metrics
        gold = len(item.missed) + len({h["gold"] for h in item.hits if h.get("gold") is not None})
        lines.append(
            f"| {item.id} | {gold} | {_num(s.get('hop_recall'))} "
            f"| {_num(s.get('hop_precision'))} | {_yes(s.get('exact'))} "
            f"| {_yes(s.get('exact_at_3'))} | {_yes(s.get('layers_hit'))} "
            f"| {_num(s.get('confidence'))} "
            f"| {'; '.join(breaks.get(item.id, [])) or errors.get(item.id, '')} |"
        )
    missed = [item for item in run.per_item if item.missed]
    if missed:
        lines += ["", "## Gold hops not reached", ""]
        for item in missed:
            lines.append(f"- **{item.id}**: {item.query}")
            lines.extend(
                f"  - {hop['layer']}: `{hop['path']}` {hop.get('symbol', '')} "
                f"L{hop['lines'][0]}-{hop['lines'][1]}"
                for hop in item.missed
            )
    return "\n".join(lines) + "\n"
