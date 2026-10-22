"""``sensei-eval``: run golden suites against retrievers, compare runs, render reports.

    sensei-eval run --suite fastapi-template --retriever random --k 10,20,50,100
    sensei-eval compare <run_a> <run_b>
    sensei-eval report <run>

Runs are written to ``evals/private/reports/<id>.json`` and ``.md`` (private) and stored in
``eval_runs`` unless ``--no-store``. A run is named by its id (looked up in the reports folder,
then the database) or by the path of its JSON report.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.exc import SQLAlchemyError

from sensei_core.config import get_settings
from sensei_core.db import make_system_database
from sensei_evals import paths
from sensei_evals.reports import compare, read_run, render_comparison, render_run, write_run
from sensei_evals.retrieval import RETRIEVERS, RepoRef, RetrieverContext
from sensei_evals.runner import DEFAULT_KS, RunRecord, run_suite
from sensei_evals.schema import GoldenItem, Suite, SuiteError, load_suite
from sensei_evals.store import load_run, save_run

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


def _db_error(error: Exception) -> str:
    """One line about a database failure (SQLAlchemy's message also echoes the statement and
    its parameters, which hold the suite's queries)."""
    cause = getattr(error, "orig", None) or error
    lines = str(cause).strip().splitlines()
    return f"{type(cause).__name__}: {lines[0] if lines else ''}"


def parse_ks(value: str) -> list[int]:
    try:
        ks = sorted({int(part) for part in value.split(",") if part.strip()})
    except ValueError:
        raise _fail(f"--k must be comma-separated integers, got {value!r}") from None
    if not ks or ks[0] < 1:
        raise _fail("--k needs at least one value, each at least 1")
    return ks


def _options(values: list[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    for value in values:
        key, sep, setting = value.partition("=")
        if not sep or not key:
            raise _fail(f"--opt must look like key=value, got {value!r}")
        options[key] = setting
    return options


def select_items(suite: Suite, repo: str | None) -> tuple[str, str, list[GoldenItem]]:
    """The items to run: those for ``repo`` (or the suite's only repository), all at one sha."""
    if repo is None:
        if len(suite.repos) != 1:
            raise _fail(f"the suite covers {', '.join(suite.repos)}: pick one with --repo")
        repo = suite.repos[0]
    items = suite.for_repo(repo)
    if not items:
        raise _fail(f"no items for repository {repo!r} (the suite has {', '.join(suite.repos)})")
    shas = sorted({item.sha for item in items})
    if len(shas) != 1:
        raise _fail(f"items for {repo} are pinned to several commits: {', '.join(shas)}")
    return repo, shas[0], items


def _checkout(root: Path, repo: str, sha: str, checkout: Path | None) -> Path | None:
    if checkout is None:
        default = paths.checkouts_dir(root) / repo
        if not default.is_dir():
            return None
        checkout = default
    if not checkout.is_dir():
        raise _fail(f"checkout {checkout} is not a directory")
    head = paths.head_sha(checkout)
    if head is None:
        typer.echo(
            f"warning: {checkout} is not a git checkout; can't confirm it is {sha}", err=True
        )
    elif head != sha:
        raise _fail(f"checkout {checkout} is at {head}, the suite is pinned to {sha}")
    return checkout


async def _store(run: RunRecord) -> None:
    database = make_system_database(get_settings().database)
    try:
        async with database.session() as session:
            await save_run(session, run)
    finally:
        await database.dispose()


async def _fetch(run_id: uuid.UUID) -> RunRecord | None:
    database = make_system_database(get_settings().database)
    try:
        async with database.session() as session:
            return await load_run(session, run_id)
    finally:
        await database.dispose()


def resolve_run(ref: str, reports: Path) -> RunRecord:
    """A run by JSON path, by id in the reports folder, or by id in the database."""
    candidate = Path(ref)
    if candidate.suffix == ".json" and candidate.is_file():
        return read_run(candidate)
    if (reports / f"{ref}.json").is_file():
        return read_run(reports / f"{ref}.json")
    try:
        run_id = uuid.UUID(ref)
    except ValueError:
        raise _fail(f"{ref!r} is neither a report file nor a run id") from None
    try:
        run = asyncio.run(_fetch(run_id))
    except (OSError, SQLAlchemyError) as error:
        raise _fail(
            f"run {ref} isn't in {reports} and reading the database failed: {_db_error(error)}"
        ) from None
    if run is None:
        raise _fail(f"no run {ref} in {reports} or the database")
    return run


@app.command()
def run(
    *,
    suite: Annotated[
        str, typer.Option(help="Suite name (evals/private/golden/<name>.yaml) or path.")
    ],
    retriever: Annotated[str, typer.Option(help=f"One of: {', '.join(RETRIEVERS)}.")],
    repo: Annotated[str | None, typer.Option(help="Benchmark repository id in the suite.")] = None,
    k: Annotated[str, typer.Option(help="Cutoffs for recall@k.")] = ",".join(map(str, DEFAULT_KS)),
    checkout: Annotated[
        Path | None,
        typer.Option(help="Checkout of the pinned commit (default .cache/eval-repos/<repo>)."),
    ] = None,
    seed: Annotated[int, typer.Option(help="Seed for randomized retrievers.")] = 0,
    opt: Annotated[
        list[str] | None, typer.Option(help="Retriever option key=value (repeatable).")
    ] = None,
    reports_dir: Annotated[Path | None, typer.Option(help="Where reports go.")] = None,
    store: Annotated[bool, typer.Option(help="Also store the run in eval_runs.")] = True,
) -> None:
    """Run a suite against a retriever, write the reports and store the run."""
    root = paths.repo_root()
    ks = parse_ks(k)
    factory = RETRIEVERS.get(retriever)
    if factory is None:
        raise _fail(f"unknown retriever {retriever!r}; known: {', '.join(RETRIEVERS)}")
    try:
        loaded = load_suite(paths.suite_path(root, suite))
    except FileNotFoundError as error:
        raise _fail(str(error)) from None
    except SuiteError as error:
        raise _fail(f"invalid suite:\n{error}") from None
    repo_id, sha, items = select_items(loaded, repo)
    ref = RepoRef(id=repo_id, sha=sha, checkout=_checkout(root, repo_id, sha, checkout))
    options = _options(opt or [])
    try:
        built = factory(RetrieverContext(suite=loaded, repo=ref, seed=seed, options=options))
    except ValueError as error:
        raise _fail(str(error)) from None
    config = {
        "seed": seed,
        "options": options,
        "items": len(items),
        "dirty": paths.has_uncommitted_changes(root),
        # Which exact suite file ran: an edit without a version bump still tells.
        "suite_sha256": hashlib.sha256(paths.suite_path(root, suite).read_bytes()).hexdigest(),
    }
    record = asyncio.run(
        run_suite(loaded, items, built, ref, ks=ks, config=config, git_sha=paths.head_sha(root))
    )
    json_path, md_path = write_run(record, reports_dir or paths.reports_dir(root))
    if store:
        try:
            asyncio.run(_store(record))
        except (OSError, SQLAlchemyError) as error:
            raise _fail(
                f"reports written, but storing the run failed ({_db_error(error)}); use --no-store"
            ) from None
    typer.echo(render_run(record))
    typer.echo(f"run {record.id}: {json_path} and {md_path}" + (" (stored)" if store else ""))


@app.command("compare")
def compare_runs(
    run_a: Annotated[str, typer.Argument(help="The baseline run (id or JSON path).")],
    run_b: Annotated[str, typer.Argument(help="The new run (id or JSON path).")],
    reports_dir: Annotated[Path | None, typer.Option(help="Where reports are.")] = None,
) -> None:
    """Compare two runs, regressions first."""
    reports = reports_dir or paths.reports_dir(paths.repo_root())
    a, b = resolve_run(run_a, reports), resolve_run(run_b, reports)
    markdown = render_comparison(compare(a, b))
    reports.mkdir(parents=True, exist_ok=True)
    out = reports / f"compare-{a.id}-{b.id}.md"
    out.write_text(markdown, encoding="utf-8")
    typer.echo(markdown)
    typer.echo(f"written to {out}")


@app.command()
def report(
    run_ref: Annotated[str, typer.Argument(metavar="RUN", help="Run id or JSON path.")],
    reports_dir: Annotated[Path | None, typer.Option(help="Where reports are.")] = None,
) -> None:
    """Print a run's markdown report (and write it next to the JSON)."""
    reports = reports_dir or paths.reports_dir(paths.repo_root())
    record = resolve_run(run_ref, reports)
    _, md_path = write_run(record, reports)
    typer.echo(render_run(record))
    typer.echo(f"written to {md_path}")
