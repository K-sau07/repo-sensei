"""The ``sensei-eval`` CLI and its reports, end to end on small suites (no database)."""

from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from sensei_evals import cli
from sensei_evals.cli import app
from sensei_evals.reports import compare, read_run, render_comparison
from sensei_evals.retrieval import OracleRetriever, RandomRetriever, RepoRef
from sensei_evals.runner import RunRecord, run_suite
from sensei_evals.schema import parse_suite

SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"
FIXTURE = (
    Path(__file__).parents[3]
    / "packages"
    / "engine"
    / "tests"
    / "fixtures"
    / "fastapi-template.tar.gz"
)
SUITE = dedent(
    f"""\
    name: cli-demo
    version: 2
    items:
      - id: login-handler
        repo: fastapi-template
        sha: {SHA}
        query: which endpoint swaps a username and password for an access token
        class: conceptual
        layers: [handler]
        gold:
          - layer: handler
            path: backend/app/api/routes/login.py
            symbol: login_access_token
            lines: [23, 42]
      - id: no-billing
        repo: fastapi-template
        sha: {SHA}
        query: how are invoices emailed to customers
        class: unanswerable
        answerable: false
    """
)

runner = CliRunner()


@pytest.fixture
def suite_file(tmp_path: Path) -> Path:
    path = tmp_path / "cli-demo.yaml"
    path.write_text(SUITE, encoding="utf-8")
    return path


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    return tmp_path / "fastapi-template"


def _run(*args: str | Path) -> tuple[int, str]:
    result = runner.invoke(app, [str(arg) for arg in args])
    return result.exit_code, result.output


def test_run_writes_json_and_markdown(suite_file: Path, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    code, output = _run(
        "run", "--suite", suite_file, "--retriever", "oracle", "--k", "5,1",
        "--reports-dir", reports, "--no-store",
    )  # fmt: skip
    assert code == 0, output
    [json_path] = reports.glob("*.json")
    run = read_run(json_path)
    assert (run.suite, run.suite_version, run.retriever, run.repo_sha) == (
        "cli-demo", 2, "oracle", SHA,
    )  # fmt: skip
    assert run.config["ks"] == [1, 5]
    assert run.metrics["overall"]["recall_symbol@5"] == 1.0
    assert run.metrics["overall"]["abstention_accuracy"] == 1.0
    assert (
        (reports / f"{run.id}.md").read_text(encoding="utf-8").startswith(f"# Eval run `{run.id}`")
    )
    assert "| recall_symbol@5 | 1.000 |" in output
    assert json.loads(json_path.read_text())["per_item"][0]["query_class"] == "conceptual"


def test_run_keeps_one_query_class(suite_file: Path, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    code, output = _run(
        "run", "--suite", suite_file, "--retriever", "oracle", "--class", "unanswerable",
        "--reports-dir", reports, "--no-store",
    )  # fmt: skip
    assert code == 0, output
    run = read_run(next(reports.glob("*.json")))
    assert [item.id for item in run.per_item] == ["no-billing"]
    assert (run.config["class"], run.config["items"]) == ("unanswerable", 1)


@pytest.mark.parametrize("retriever", ["random", "symbols"])
def test_retrievers_that_read_files_need_a_checkout(
    suite_file: Path, tmp_path: Path, retriever: str
) -> None:
    # A repository id with no checkout under .cache/eval-repos/.
    suite_file.write_text(SUITE.replace("fastapi-template", "no-checkout-here"), encoding="utf-8")
    code, output = _run(
        "run", "--suite", suite_file, "--retriever", retriever, "--reports-dir", tmp_path,
        "--no-store", "--repo", "no-checkout-here",
    )  # fmt: skip
    assert code == 1
    assert "needs a checkout" in output


def test_random_runs_on_a_plain_checkout(suite_file: Path, checkout: Path, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    code, output = _run(
        "run", "--suite", suite_file, "--retriever", "random", "--checkout", checkout,
        "--reports-dir", reports, "--no-store", "--seed", "5",
    )  # fmt: skip
    assert code == 0, output
    assert "is not a git checkout" in output  # a tarball can't prove its commit
    run = read_run(next(reports.glob("*.json")))
    assert run.config["seed"] == 5
    assert run.metrics["overall"]["abstention_accuracy"] == 0.5  # random always answers


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--retriever", "nope"], "unknown retriever 'nope'"),
        (["--retriever", "oracle", "--k", "ten"], "--k must be comma-separated integers"),
        (["--retriever", "oracle", "--k", "0"], "each at least 1"),
        (["--retriever", "oracle", "--repo", "langflow"], "no items for repository 'langflow'"),
        (["--retriever", "oracle", "--opt", "novalue"], "--opt must look like key=value"),
        (["--retriever", "oracle", "--class", "regex"], "no regex items for repository"),
    ],
)
def test_run_rejects_bad_arguments(
    suite_file: Path, tmp_path: Path, args: list[str], message: str
) -> None:
    code, output = _run(
        "run", "--suite", suite_file, *args, "--reports-dir", tmp_path, "--no-store"
    )
    assert code == 1
    assert message in output


def test_invalid_suite_points_at_the_line(suite_file: Path, tmp_path: Path) -> None:
    suite_file.write_text(SUITE.replace("lines: [23, 42]", "lines: [42, 23]"), encoding="utf-8")
    code, output = _run(
        "run",
        "--suite",
        suite_file,
        "--retriever",
        "oracle",
        "--reports-dir",
        tmp_path,
        "--no-store",
    )
    assert code == 1
    assert f"{suite_file}:11: items.0.gold.0: lines must be" in output


def test_multi_repo_suite_needs_repo(suite_file: Path, tmp_path: Path) -> None:
    suite_file.write_text(
        SUITE.replace("repo: fastapi-template", "repo: other", 1), encoding="utf-8"
    )
    code, output = _run(
        "run",
        "--suite",
        suite_file,
        "--retriever",
        "oracle",
        "--reports-dir",
        tmp_path,
        "--no-store",
    )
    assert code == 1
    assert "pick one with --repo" in output


def test_missing_suite(tmp_path: Path) -> None:
    code, output = _run(
        "run", "--suite", tmp_path / "nope.yaml", "--retriever", "oracle", "--no-store"
    )
    assert code == 1
    assert "nope.yaml" in output


async def test_compare_lists_regressions_first(checkout: Path) -> None:
    suite = parse_suite(SUITE)
    repo = RepoRef(id="fastapi-template", sha=SHA, checkout=checkout)
    oracle = await run_suite(suite, suite.items, OracleRetriever(suite.items), repo, ks=[10])
    empty = RandomRetriever([])  # an empty corpus: finds nothing for any query
    nothing = await run_suite(suite, suite.items, empty, repo, ks=[10])
    comparison = compare(oracle, nothing)
    assert comparison.metric == "recall_symbol@10"
    assert comparison.regressions[0].delta == -1.0
    assert [c.name for c in comparison.improvements] == []
    # The empty retriever abstains everywhere: right on the unanswerable item only.
    abstention = next(c for c in comparison.regressions if c.name == "abstention_accuracy")
    assert (abstention.before, abstention.after) == (1.0, 0.5)
    assert [(c.id, c.before, c.after) for c in comparison.items_worse] == [
        ("login-handler", 1.0, 0.0)
    ]
    markdown = render_comparison(comparison)
    assert markdown.index("## Regressions") < markdown.index("## Items that got worse")
    assert "⚠" not in markdown

    swapped = compare(nothing, oracle)
    assert swapped.regressions == []
    assert swapped.improvements[0].delta == 1.0

    # Runs that aren't alike say so: other depths, or the suite file edited in place.
    deeper = await run_suite(
        suite,
        suite.items,
        OracleRetriever(suite.items),
        repo,
        ks=[10, 50],
        config={"suite_sha256": "b" * 64},
    )
    edited = oracle.model_copy(update={"config": {**oracle.config, "suite_sha256": "a" * 64}})
    warnings = compare(edited, deeper).warnings
    assert "the suite file changed between the runs (same name and version)" in warnings
    assert "different ks: [10] vs [10, 50]" in warnings


def test_compare_and_report_commands(suite_file: Path, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    for k in ("10", "1"):
        code, output = _run(
            "run", "--suite", suite_file, "--retriever", "oracle", "--k", k,
            "--reports-dir", reports, "--no-store",
        )  # fmt: skip
        assert code == 0, output
    first, second = sorted(reports.glob("*.json"))  # uuid7 ids sort by creation
    code, output = _run("compare", first, second.stem, "--reports-dir", reports)
    assert code == 0, output
    assert f"# Compare `{first.stem}` → `{second.stem}`" in output
    assert (reports / f"compare-{first.stem}-{second.stem}.md").is_file()

    (reports / f"{second.stem}.md").unlink()
    code, output = _run("report", second.stem, "--reports-dir", reports)
    assert code == 0, output
    assert (reports / f"{second.stem}.md").is_file()


def test_unknown_run_reference(tmp_path: Path) -> None:
    code, output = _run("report", "not-a-run", "--reports-dir", tmp_path)
    assert code == 1
    assert "neither a report file nor a run id" in output


def test_checkout_at_another_commit_is_rejected(suite_file: Path, tmp_path: Path) -> None:
    repo = tmp_path / "wrong"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([*git, "add", "a.py"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "-m", "one"], check=True)  # noqa: S603
    code, output = _run(
        "run", "--suite", suite_file, "--retriever", "random", "--checkout", repo,
        "--reports-dir", tmp_path, "--no-store",
    )  # fmt: skip
    assert code == 1
    assert f"the suite is pinned to {SHA}" in output


def test_store_command_stores_written_runs(
    suite_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = tmp_path / "reports"
    code, output = _run(
        "run", "--suite", suite_file, "--retriever", "oracle", "--reports-dir", reports,
        "--no-store",
    )  # fmt: skip
    assert code == 0, output
    [json_path] = reports.glob("*.json")
    stored: list[RunRecord] = []

    async def store(run: RunRecord) -> None:
        stored.append(run)

    monkeypatch.setattr(cli, "_store", store)
    code, output = _run("store", json_path)
    assert code == 0, output
    assert [run.id for run in stored] == [read_run(json_path).id]
    assert f"stored run {stored[0].id} (cli-demo, oracle)" in output
    code, output = _run("store", tmp_path / "missing.json")
    assert code == 1
    assert "no report" in output
