"""CrossLayerBench (3.13): the bench file, scoring a trace hop by hop, calibration, 3.12's
answers, runs like a suite's, renamed copies' flows, the CLI and the label helper."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from sensei_engine.graph.snapshot import GraphSnapshot
from sensei_engine.graph.trace import TraceResult
from sensei_evals import paths, tracing
from sensei_evals.cli import app
from sensei_evals.crosslayer import (
    Bench,
    Flow,
    GoldTracer,
    TraceHop,
    TracePath,
    TracerContext,
    as_suite,
    bench_path,
    calibration,
    parse_bench,
    paths_from_trace,
    renamed_flows,
    run_bench,
    score_flow,
    score_path,
)
from sensei_evals.rename import commit_tree, make_plan, rename_tree, write_mapping
from sensei_evals.reports import compare, read_run, render_run
from sensei_evals.repos import mapping_path
from sensei_evals.schema import QueryClass, SuiteError

ROOT = Path(__file__).parents[3]
SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"

BENCH = dedent(
    f"""\
    name: crosslayerbench
    version: v0
    flows:
      - id: ft-add-item
        repo: fastapi-template
        sha: {SHA}
        title: adding an item from the dialog to the stored row
        hops:
          - {{layer: ui, path: web/AddItem.tsx, symbol: AddItem, lines: [10, 40]}}
          - {{layer: client, path: web/sdk.ts, symbol: ItemsService.createItem, lines: [5, 9]}}
          - {{layer: handler, path: api/items.py, symbol: create_item, lines: [20, 30]}}
          - {{layer: db, path: api/models.py, symbol: Item, lines: [50, 60]}}
      - id: ft-login
        repo: fastapi-template
        sha: {SHA}
        title: logging in from the login page
        hops:
          - {{layer: ui, path: web/Login.tsx, symbol: Login, lines: [1, 30]}}
          - {{layer: handler, path: api/login.py, symbol: login, lines: [3, 9]}}
    """
)


def _bench() -> Bench:
    return parse_bench(BENCH)


def _hop(path: str | None, lines: tuple[int, int] | None, **rest: Any) -> TraceHop:
    return TraceHop(kind=rest.pop("kind", "function"), name="x", path=path, lines=lines, **rest)


# ---------------------------------------------------------------------- the file


def test_a_bench_reads_as_a_suite_of_flow_items() -> None:
    bench = _bench()
    assert (bench.number, bench.repos) == (0, ["fastapi-template"])
    flow = bench.flows[0]
    assert flow.entry.symbol == "AddItem"
    assert [layer.value for layer in flow.layers] == ["ui", "client", "handler", "db"]
    suite = as_suite(bench)
    assert (suite.name, suite.version) == ("crosslayerbench-v0", 0)
    assert [item.class_ for item in suite.items] == [QueryClass.FLOW] * 2
    assert suite.items[0].gold == flow.hops


LOGIN_HOP = "path: api/login.py, symbol: login, lines: [3, 9]"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (f"      - {{layer: handler, {LOGIN_HOP}}}\n", "", "a flow needs at least two hops"),
        ("symbol: login, ", "", "every hop names its symbol"),
        (LOGIN_HOP, "path: web/Login.tsx, symbol: Login, lines: [1, 30]", "a hop repeats"),
        ("id: ft-login", "id: ft-add-item", "flow ids repeat: ft-add-item"),
        ("version: v0", "version: 0", "version"),
    ],
)
def test_bad_benches_say_why(old: str, new: str, message: str) -> None:
    assert old in BENCH
    with pytest.raises(SuiteError, match=message):
        parse_bench(BENCH.replace(old, new))


def test_a_flow_may_say_the_query_a_user_would_type_and_the_tracer_follow_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``query`` is what finds a flow's feature on a page with several; ``--opt feature=query``
    has the trace tracer follow it (and nothing else is a ``feature=``)."""
    bench = parse_bench(BENCH.replace("    hops:\n", "    query: add item\n    hops:\n", 1))
    assert [flow.query for flow in bench.flows] == ["add item", None]
    asked: list[str | None] = []

    def traced(graph: GraphSnapshot, entry: str, **options: Any) -> TraceResult:
        asked.append(options.get("feature"))
        return TraceResult([], None, [])

    monkeypatch.setattr(tracing, "trace_feature", traced)

    def linked(_root: Path) -> GraphSnapshot:
        return GraphSnapshot([], [])

    monkeypatch.setattr(tracing, "link_checkout", linked)

    def tracer(**options: str) -> tracing.TraceTracer:
        return tracing.TraceTracer.build(TracerContext("fastapi-template", SHA, tmp_path, options))

    by_query, plain = tracer(feature="query"), tracer()
    for flow in bench.flows:
        assert by_query.trace(flow) == []
        assert plain.trace(flow) == []
    assert asked == ["add item", None, None, None]
    with pytest.raises(ValueError, match="takes 'query'"):
        tracer(feature="x")


def test_bench_names_and_paths() -> None:
    root = Path("/r")
    assert bench_path(root, "crosslayerbench-v0") == Path(
        "/r/evals/private/crosslayerbench/v0.yaml"
    )
    assert bench_path(root, "x/crosslayerbench/v1.yaml") == Path("x/crosslayerbench/v1.yaml")
    assert bench_path(root, "fastapi-template") is None
    assert bench_path(root, "x/golden/v1.yaml") is None


# ---------------------------------------------------------------------- scoring


def test_the_gold_path_is_exact() -> None:
    flow = _bench().flows[0]
    [path] = GoldTracer().trace(flow)
    score = score_path(path, flow.hops)
    assert (score.hop_recall, score.hop_precision, score.exact, score.layers_hit) == (
        1.0, 1.0, True, True,
    )  # fmt: skip


def test_hops_match_by_overlapping_span_and_tables_by_their_model() -> None:
    gold = _bench().flows[0].hops
    path = TracePath(
        (
            _hop("web/AddItem.tsx", (10, 40), kind="component"),
            _hop("web/AddItem.tsx", (30, 31), kind="http_client_call"),  # inside the entry
            _hop("web/sdk.ts", (1, 6)),  # overlaps the client hop
            _hop(None, None, kind="api_endpoint"),  # no place: neither right nor wrong
            _hop("api/items.py", (20, 30)),
            _hop(None, None, kind="table", model=("api/models.py", (50, 50))),
        ),
        0.5,
    )
    score = score_path(path, gold)
    assert score.matched == (0, 0, 1, None, 2, 3)
    assert (score.hop_recall, score.hop_precision, score.exact) == (1.0, 1.0, True)


def test_a_wrong_or_missing_hop_is_not_exact() -> None:
    gold = _bench().flows[0].hops
    detour = TracePath(
        (
            _hop("web/AddItem.tsx", (10, 40)),
            _hop("web/other.ts", (1, 2)),  # a hop the gold doesn't have
            _hop("web/sdk.ts", (5, 9)),
            _hop("api/items.py", (20, 30)),
            _hop("api/models.py", (50, 60)),
        ),
        0.4,
    )
    score = score_path(detour, gold)
    assert (score.hop_recall, score.hop_precision, score.exact) == (1.0, 0.8, False)
    short = TracePath((_hop("web/AddItem.tsx", (10, 40)), _hop("web/sdk.ts", (5, 9))), 0.9)
    score = score_path(short, gold)
    assert (score.hop_recall, score.exact, score.layers_hit) == (0.5, False, False)
    backwards = TracePath(
        tuple(_hop(h.path, h.lines) for h in reversed(gold)), 0.9
    )  # every hop, out of order
    assert not score_path(backwards, gold).exact


def test_a_flow_scores_its_best_path_and_any_of_three_for_exact() -> None:
    flow = _bench().flows[0]
    [gold] = GoldTracer().trace(flow)
    short = TracePath(gold.hops[:2], 0.8, ("after hop 1: client call has no endpoint",))
    result = score_flow(flow, [short, gold])
    assert (result.exact, result.exact_at_3, result.confidence) == (False, True, 0.8)
    assert result.breaks == ["after hop 1: client call has no endpoint"]
    assert result.hop_confidences == [(1.0, True)]  # the entry has no edge into it
    untraced = score_flow(flow, [])
    assert (untraced.paths, untraced.hop_recall, untraced.hop_precision) == (0, 0.0, None)


def test_calibration() -> None:
    found = calibration([(0.9, True), (0.9, False), (0.15, False), (1.0, True)])
    # Bins: [0.9, 1.0) holds 0.9, 0.9 and 1.0 (the top bin is closed): mean 0.933, right 2/3;
    # [0.1, 0.2) holds 0.15, never right.
    assert found["ece"] == round(0.75 * abs(2.8 / 3 - 2 / 3) + 0.25 * 0.15, 4)
    assert found["brier"] == round((0.01 + 0.81 + 0.0225 + 0.0) / 4, 4)
    assert [b["n"] for b in found["bins"]] == [1, 3]
    assert calibration([]) == {"n": 0, "ece": None, "brier": None, "bins": []}


def test_trace_feature_answers_become_paths() -> None:
    answer = {
        "paths": [
            {
                "hops": [
                    {
                        "layer": "ui",
                        "node": {
                            "kind": "component",
                            "name": "AddItem",
                            "path": "web/AddItem.tsx",
                            "lines": [10, 40],
                        },
                        "edge": None,
                    },
                    {
                        "layer": "data",
                        "node": {
                            "kind": "table",
                            "name": "item",
                            "path": None,
                            "lines": None,
                            "model": {
                                "symbol_key": "py api/models.py/Item#",
                                "path": "api/models.py",
                                "lines": [50, 50],
                            },
                        },
                        "edge": {"type": "WRITES", "confidence": 0.7},
                    },
                ],
                "path_confidence": 0.7,
                "breaks": [{"after_hop": 0, "reason": "client call has no matching endpoint"}],
            }
        ]
    }
    [path] = paths_from_trace(answer)
    entry, table = path.hops
    assert (entry.confidence, entry.lines, entry.layer) == (None, (10, 40), "ui")
    assert (table.confidence, table.model, table.placed) == (0.7, ("api/models.py", (50, 50)), True)
    assert path.breaks == ("after hop 0: client call has no matching endpoint",)
    assert paths_from_trace({}) == []


# ---------------------------------------------------------------------- runs


class _Failing:
    name = "failing"

    def trace(self, flow: Flow) -> list[TracePath]:
        if flow.id == "ft-login":
            msg = "no index"
            raise RuntimeError(msg)
        return list(GoldTracer().trace(flow))


def test_a_run_is_a_record_like_a_suites() -> None:
    bench = _bench()
    run = run_bench(bench, GoldTracer(), bench.flows, repo_sha=SHA, config={"items": 2})
    overall = run.metrics["overall"]
    assert (run.suite, run.suite_version, run.retriever, run.repo_sha) == (
        "crosslayerbench-v0", 0, "gold", SHA,
    )  # fmt: skip
    assert {overall[k] for k in ("hop_recall", "hop_precision", "exact", "layers_hit")} == {1.0}
    assert overall["path_ece"] == 0.0
    assert run.metrics["calibration"]["hops"]["n"] == 4  # 3 + 1 edges
    assert [item.query_class for item in run.per_item] == ["flow", "flow"]
    assert run.per_item[0].missed == []
    report = render_run(run)  # the suite report hands a bench's to its own
    assert report.startswith("# crosslayerbench-v0: `gold`")
    assert "| ft-add-item | 4 | 1.000 | 1.000 | yes | yes | yes | 1.000 |  |" in report

    failed = run_bench(bench, _Failing(), bench.flows, repo_sha=SHA)
    assert failed.metrics["errors"] == {"ft-login": "RuntimeError: no index"}
    assert failed.metrics["overall"]["hop_recall"] == 0.5
    login = failed.per_item[1]
    assert (login.abstained, len(login.missed)) == (True, 2)
    assert "Gold hops not reached" in render_run(failed)
    change = compare(run, failed)
    assert change.metric == "hop_recall"
    assert [(c.id, c.before, c.after) for c in change.items_worse] == [("ft-login", 1.0, 0.0)]


# ---------------------------------------------------------------------- renamed copies


def _git_commit(directory: Path) -> str:
    git = ["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "snapshot"]):
        subprocess.run([*git, *args], check=True)  # noqa: S603
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)  # noqa: S603
    return head.stdout.strip()


DEMO = {
    "web/Widgets/WidgetsTable.tsx": (
        'import { readWidgets } from "../../api/widgets"\n\n'
        "export function WidgetsTable() {\n  return readWidgets()\n}\n"
    ),
    "api/widgets.py": 'def read_widgets():\n    """Every widget."""\n    return []\n',
}


def _demo_bench(sha: str) -> str:
    return dedent(
        f"""\
        name: crosslayerbench
        version: v0
        flows:
          - id: demo-widgets
            repo: demo
            sha: {sha}
            title: listing the widgets in `WidgetsTable`
            hops:
              - {{layer: ui, path: web/Widgets/WidgetsTable.tsx, symbol: WidgetsTable,
                 lines: [3, 5]}}
              - {{layer: handler, path: api/widgets.py, symbol: read_widgets, lines: [1, 3]}}
        """
    )


@pytest.fixture
def private_root(tmp_path: Path) -> tuple[Path, str]:
    """A root with a pinned checkout ``demo``, a renamed copy of it and a bench for it."""
    root = tmp_path / "root"
    checkouts = paths.checkouts_dir(root)
    for name, text in DEMO.items():
        (checkouts / "demo" / name).parent.mkdir(parents=True, exist_ok=True)
        (checkouts / "demo" / name).write_text(text, encoding="utf-8")
    sha = _git_commit(checkouts / "demo")
    paths.repos_file(root).parent.mkdir(parents=True)
    paths.repos_file(root).write_text(
        f"repos:\n  - id: demo\n    url: https://example.com/demo\n    sha: {sha}\n",
        encoding="utf-8",
    )
    plan = make_plan(["Widgets", "WidgetsTable", "widgets", "readWidgets", "read_widgets"], set())
    result = rename_tree(checkouts / "demo", checkouts / "demo-renamed", plan)
    result.sha, result.repo = commit_tree(checkouts / "demo-renamed"), "demo-renamed"
    result.source = {"repo": "demo", "sha": sha}
    write_mapping(result, mapping_path(checkouts, "demo-renamed"))
    bench = paths.private_dir(root) / "crosslayerbench" / "v0.yaml"
    bench.parent.mkdir(parents=True)
    bench.write_text(_demo_bench(sha), encoding="utf-8")
    return root, sha


def test_renamed_copies_get_the_flows_translated(private_root: tuple[Path, str]) -> None:
    root, sha = private_root
    checkouts = paths.checkouts_dir(root)
    bench = parse_bench(_demo_bench(sha))
    [flow], problems = renamed_flows(bench, checkouts)
    assert problems == []
    mapping = json.loads(mapping_path(checkouts, "demo-renamed").read_text(encoding="utf-8"))
    table = mapping["tokens"]["WidgetsTable"]
    assert (flow.id, flow.repo, flow.sha) == (
        "demo-widgets.renamed",
        "demo-renamed",
        mapping["sha"],
    )
    assert flow.title == f"listing the widgets in `{table}`"
    assert flow.hops[0].symbol == table
    assert flow.hops[0].path == mapping["paths"]["web/Widgets/WidgetsTable.tsx"]
    assert [hop.lines for hop in flow.hops] == [(3, 5), (1, 3)]

    stale = parse_bench(_demo_bench("a" * 40))
    flows, problems = renamed_flows(stale, checkouts)
    assert flows == []
    assert problems == [
        f"demo-renamed was made from demo at {sha}, its flows are pinned to {'a' * 40}: "
        "re-run rename"
    ]


def _label_helper_validate() -> Callable[..., int]:
    spec = importlib.util.spec_from_file_location("label_helper", ROOT / "scripts/label_helper.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast("Callable[..., int]", module.validate)


def test_label_helper_checks_bench_hops_on_the_repo_and_its_copies(
    private_root: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = private_root
    validate = _label_helper_validate()
    code = validate([], False, root=root)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "1 suites, 2 items: 0 errors, 0 warnings" in out  # no class-mix warning for flows

    bench = paths.private_dir(root) / "crosslayerbench" / "v0.yaml"
    bench.write_text(
        bench.read_text(encoding="utf-8").replace(
            "read_widgets, lines: [1, 3]", "read_widgets, lines: [2, 3]"
        ),
        encoding="utf-8",
    )
    code = validate([], False, root=root)
    out = capsys.readouterr().out
    assert code == 1
    assert "demo-widgets:" in out
    assert "demo-widgets.renamed:" in out  # the copy's flow is checked too


def test_cli_runs_a_bench_for_a_repo_and_its_renamed_copy(
    private_root: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sha = private_root
    (root / "packages" / "evals").mkdir(parents=True)
    (root / "packages" / "evals" / "pyproject.toml").touch()  # what marks a checkout's root
    monkeypatch.chdir(root)
    reports = tmp_path / "reports"

    def run(*args: str) -> tuple[int, str]:
        result = CliRunner().invoke(
            app,
            ["run", "--suite", "crosslayerbench-v0", "--reports-dir", str(reports),
             "--no-store", *args],
        )  # fmt: skip
        return result.exit_code, result.output

    code, output = run("--retriever", "gold")
    assert code == 0, output
    [json_path] = reports.glob("*.json")
    record = read_run(json_path)
    assert (record.repo_sha, record.config["repo"], record.metrics["overall"]["exact"]) == (
        sha, "demo", 1.0,
    )  # fmt: skip
    assert "| demo-widgets | 2 | 1.000 |" in output
    json_path.unlink()

    code, output = run("--retriever", "gold", "--repo", "demo-renamed")
    assert code == 0, output
    [json_path] = reports.glob("*.json")
    assert read_run(json_path).per_item[0].id == "demo-widgets.renamed"
    json_path.unlink()

    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"demo-widgets": {"paths": []}}), encoding="utf-8")
    code, output = run("--retriever", "answers", "--opt", f"answers={answers}")
    assert code == 0, output
    [json_path] = reports.glob("*.json")
    assert read_run(json_path).metrics["overall"]["traced"] == 0.0

    code, output = run("--retriever", "nope")
    assert code == 1
    assert "unknown tracer 'nope'; known: gold, answers" in output
    code, output = run("--retriever", "answers")
    assert code == 1
    assert "needs --opt answers=" in output
