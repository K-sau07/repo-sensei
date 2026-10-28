"""``sensei-eval``: run golden suites against retrievers, compare runs, render reports.

    sensei-eval run --suite fastapi-template --retriever random --k 10,20,50,100
    sensei-eval compare <run_a> <run_b>
    sensei-eval report <run>
    sensei-eval store evals/private/reports/<id>.json
    sensei-eval rename --suite fastapi-template --repo fastapi-template
    sensei-eval prepare-db && sensei-eval run --suite fastapi-template --retriever symbols \\
        --class symbol
    sensei-eval route --suite fastapi-template --suite langflow \\
        --extra evals/private/router/queries.yaml
    sensei-eval run --suite crosslayerbench-v0 --repo fastapi-template --retriever gold

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
from sensei_core.usage import CostCalculator
from sensei_engine.llm.anthropic_client import AnthropicClient
from sensei_engine.search.route import QueryRouter
from sensei_evals import crosslayer, paths, routing
from sensei_evals import rename as rename_mod
from sensei_evals.reports import compare, read_run, render_comparison, render_run, write_run
from sensei_evals.repos import mapping_path, valid_repo_id
from sensei_evals.retrieval import RETRIEVERS, RepoRef, RetrieverContext
from sensei_evals.runner import DEFAULT_KS, RunRecord, run_suite
from sensei_evals.schema import (
    GoldenItem,
    QueryClass,
    RenamedFrom,
    Suite,
    SuiteError,
    load_suite,
    parse_suite,
)
from sensei_evals.store import load_run, save_run
from sensei_evals.symbols import EVAL_DATABASE, migrate_database, recreate_database

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
    elif paths.has_uncommitted_changes(checkout):
        raise _fail(f"checkout {checkout} has local changes (git status isn't clean)")
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
    query_class: Annotated[
        QueryClass | None,
        typer.Option("--class", help="Only the items of this query class (symbol, regex, ...)."),
    ] = None,
    reports_dir: Annotated[Path | None, typer.Option(help="Where reports go.")] = None,
    store: Annotated[bool, typer.Option(help="Also store the run in eval_runs.")] = True,
) -> None:
    """Run a suite against a retriever, write the reports and store the run. A CrossLayerBench
    (``--suite crosslayerbench-v0``) runs its flows for one repository (or a renamed copy of
    one) against a tracer instead (``--retriever gold``, ``answers``, ...)."""
    root = paths.repo_root()
    bench_file = crosslayer.bench_path(root, suite)
    if bench_file is not None:
        _run_bench(
            root,
            bench_file,
            tracer=retriever,
            repo=repo,
            checkout=checkout,
            options=_options(opt or []),
            reports=reports_dir or paths.reports_dir(root),
            store=store,
        )
        return
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
    if query_class is not None:
        items = [item for item in items if item.class_ is query_class]
        if not items:
            raise _fail(f"no {query_class.value} items for repository {repo_id!r}")
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
        "class": query_class.value if query_class is not None else None,
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


def _run_bench(
    root: Path,
    bench_file: Path,
    *,
    tracer: str,
    repo: str | None,
    checkout: Path | None,
    options: dict[str, str],
    reports: Path,
    store: bool,
) -> None:
    factory = crosslayer.TRACERS.get(tracer)
    if factory is None:
        raise _fail(f"unknown tracer {tracer!r}; known: {', '.join(crosslayer.TRACERS)}")
    try:
        bench = crosslayer.load_bench(bench_file)
    except FileNotFoundError as error:
        raise _fail(str(error)) from None
    except SuiteError as error:
        raise _fail(f"invalid bench:\n{error}") from None
    if repo is not None and repo not in bench.repos:  # a renamed copy's: derive its flows
        folder = checkout.parent if checkout is not None else paths.checkouts_dir(root)
        bench, problems = crosslayer.with_renamed(bench, folder)
        for problem in problems:
            typer.echo(f"warning: renamed flows: {problem}", err=True)
    repo_id, sha, items = select_items(crosslayer.as_suite(bench), repo)
    wanted = {item.id for item in items}
    flows = [flow for flow in bench.flows if flow.id in wanted]
    ref = _checkout(root, repo_id, sha, checkout)
    try:
        built = factory(crosslayer.TracerContext(repo_id, sha, ref, options))
    except ValueError as error:
        raise _fail(str(error)) from None
    config = {
        "options": options,
        "items": len(flows),
        "dirty": paths.has_uncommitted_changes(root),
        "suite_sha256": hashlib.sha256(bench_file.read_bytes()).hexdigest(),
    }
    record = crosslayer.run_bench(
        bench, built, flows, repo_sha=sha, config=config, git_sha=paths.head_sha(root)
    )
    json_path, md_path = write_run(record, reports)
    if store:
        try:
            asyncio.run(_store(record))
        except (OSError, SQLAlchemyError) as error:
            raise _fail(
                f"reports written, but storing the run failed ({_db_error(error)}); use --no-store"
            ) from None
    typer.echo(render_run(record))
    typer.echo(f"run {record.id}: {json_path} and {md_path}" + (" (stored)" if store else ""))


@app.command("sweep")
def sweep_weights(
    *,
    suite: Annotated[list[str], typer.Option(help="Suites to weigh on (repeatable).")],
    checkouts: Annotated[
        Path | None,
        typer.Option(help="Directory of the checkouts (default .cache/eval-repos)."),
    ] = None,
    opt: Annotated[
        list[str] | None, typer.Option(help="Hybrid retriever option key=value (repeatable).")
    ] = None,
    reports_dir: Annotated[Path | None, typer.Option(help="Where the report goes.")] = None,
    top: Annotated[int, typer.Option(help="Grid points listed.")] = 15,
    grid: Annotated[
        list[str] | None,
        typer.Option(
            help="A weight's values, name=v1,v2 (repeatable; replaces the default grid, the "
            "other weights kept at --opt's or the defaults)."
        ),
    ] = None,
) -> None:
    """Weigh the hybrid retriever's legs (step 2.9.4) and compare it with each leg (2.9.5): the
    rankings are read once per question, then every grid point is fused in memory and scored."""
    from sensei_evals import sweep as sweep_mod  # noqa: PLC0415 - heavy, only for this command
    from sensei_evals.hybrid import checkout_hybrid, weights_from  # noqa: PLC0415

    root = paths.repo_root()
    options = _options(opt or [])
    base = weights_from(options)
    collected: dict[str, list[sweep_mod.Collected]] = {}
    for name in suite:
        loaded = load_suite(paths.suite_path(root, name))
        repo_id, sha, items = select_items(loaded, None)
        folder = (checkouts or paths.checkouts_dir(root)) / repo_id
        ref = RepoRef(id=repo_id, sha=sha, checkout=_checkout(root, repo_id, sha, folder))
        built = checkout_hybrid(RetrieverContext(suite=loaded, repo=ref, options=options))

        collected[loaded.name] = asyncio.run(sweep_mod.collect(built, items, ref))
        typer.echo(f"{loaded.name}: {len(collected[loaded.name])} questions read")
    values = sweep_mod.GRID
    if grid:
        values = {
            name: [float(v) for v in setting.split(",")] for name, setting in _options(grid).items()
        }
    points = sweep_mod.sweep(collected, base, values=values)
    chosen = points[0].weights
    report = sweep_mod.render(sweep_mod.compare(collected, chosen), points[:top], chosen)
    folder = reports_dir or paths.reports_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"sweep-{uuid.uuid4().hex[:8]}.md"
    path.write_text(report)
    typer.echo(report)
    typer.echo(f"report: {path}")


@app.command("store")
def store_runs(
    runs: Annotated[list[Path], typer.Argument(help="JSON reports of runs made with --no-store.")],
) -> None:
    """Store runs from their JSON reports in eval_runs (for runs made while the database
    couldn't take them)."""
    records: list[RunRecord] = []
    for path in runs:
        if not path.is_file():
            raise _fail(f"no report {path}")
        records.append(read_run(path))
    for record in records:
        try:
            asyncio.run(_store(record))
        except (OSError, SQLAlchemyError) as error:
            raise _fail(f"storing run {record.id} failed: {_db_error(error)}") from None
        typer.echo(f"stored run {record.id} ({record.suite}, {record.retriever})")


@app.command("prepare-db")
def prepare_db(
    name: Annotated[str, typer.Option(help="The eval database's name.")] = EVAL_DATABASE,
) -> None:
    """Recreate the throwaway database the symbols retriever indexes into, at the latest
    schema (on the settings' server; it never is the app's own database)."""
    alembic_ini = paths.repo_root() / "packages" / "core" / "alembic.ini"
    try:
        url = asyncio.run(recreate_database(name))
        migrate_database(alembic_ini, url)
    except (ValueError, RuntimeError) as error:
        raise _fail(str(error)) from None
    except (OSError, SQLAlchemyError) as error:
        raise _fail(f"recreating {name} failed: {_db_error(error)}") from None
    typer.echo(f"{name}: ready")


@app.command("route")
def route_queries(
    suite: Annotated[
        list[str], typer.Option(help="Suite name or path (repeatable): its items' class labels.")
    ],
    extra: Annotated[
        list[Path] | None, typer.Option(help="YAML list of {class, query} (repeatable).")
    ] = None,
    llm: Annotated[bool, typer.Option(help="Ask the model (else rules only, free).")] = True,
    reports_dir: Annotated[Path | None, typer.Option(help="Where reports go.")] = None,
    store: Annotated[bool, typer.Option(help="Also store the run in eval_runs.")] = True,
) -> None:
    """How often the query router picks each item's labelled class (2.10); written as
    ``route-<mode>.md`` and ``.json``, and stored like a retrieval run."""
    root = paths.repo_root()
    queries: list[routing.LabelledQuery] = []
    try:
        for name in suite:
            queries += routing.suite_queries(load_suite(paths.suite_path(root, name)))
        for path in extra or []:
            queries += routing.extra_queries(path)
    except (SuiteError, ValueError, KeyError, OSError) as exc:
        raise _fail(str(exc)) from exc
    settings = get_settings()
    anthropic = (
        settings.anthropic if llm else settings.anthropic.model_copy(update={"api_key": None})
    )
    if llm and not anthropic.configured:
        raise _fail("--llm needs SENSEI_ANTHROPIC_API_KEY (or pass --no-llm)")
    router = QueryRouter(AnthropicClient(anthropic))
    calculator = CostCalculator.from_settings(settings.pricing)
    mode = "llm" if llm else "rules"
    report = asyncio.run(routing.evaluate(router, queries, calculator, mode=mode))
    markdown = routing.render(report)
    reports = reports_dir or paths.reports_dir(root)
    reports.mkdir(parents=True, exist_ok=True)
    out = reports / f"route-{mode}.md"
    out.write_text(markdown, encoding="utf-8")
    sources = [*suite, *(str(path) for path in extra or [])]
    record = routing.to_record(report, sources=sources, git_sha=paths.head_sha(root))
    json_path = reports / f"route-{mode}.json"
    json_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(markdown)
    if store:
        try:
            asyncio.run(_store(record))
        except (OSError, SQLAlchemyError) as error:
            raise _fail(
                f"reports written, but storing the run failed ({_db_error(error)}); use --no-store"
            ) from None
    typer.echo(f"run {record.id}: {out} and {json_path}" + (" (stored)" if store else ""))


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


@app.command()
def rename(
    *,
    suite: Annotated[str, typer.Option(help="Suite whose gold is translated (name or path).")],
    repo: Annotated[str, typer.Option(help="Benchmark repository id to copy.")],
    checkout: Annotated[
        Path | None,
        typer.Option(help="Checkout of the pinned commit (default .cache/eval-repos/<repo>)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Where the copy goes (default .cache/eval-repos/<repo>-renamed). Its folder "
            "name is the copy's repository id; the mapping is written beside it, where "
            "`label_helper.py validate --checkouts <its parent>` finds it."
        ),
    ] = None,
    symbols: Annotated[
        Path | None,
        typer.Option(help="Names to rename, one per line (instead of --names-from)."),
    ] = None,
    names_from: Annotated[
        rename_mod.NameSource,
        typer.Option(
            help="Where the repository's own names come from: the indexer's tree-sitter "
            "symbol table, or the labeling parsers."
        ),
    ] = rename_mod.NameSource.EXTRACTED,
    seed: Annotated[int, typer.Option(help="Seed for the generated names.")] = 0,
    suite_out: Annotated[
        Path | None,
        typer.Option(
            help="Translated suite file (default evals/private/golden/<suite>-renamed.yaml). "
            "Keep it beside the source suite: validation reads the source from there."
        ),
    ] = None,
) -> None:
    """Make a renamed copy of a repository, its mapping file and the translated suite."""
    root = paths.repo_root()
    suite_file = paths.suite_path(root, suite)
    try:
        raw = suite_file.read_bytes()
        loaded = parse_suite(raw.decode("utf-8"), str(suite_file))
    except FileNotFoundError:
        raise _fail(f"no suite file {suite_file}") from None
    except UnicodeDecodeError:
        raise _fail(f"suite file {suite_file} is not UTF-8") from None
    except SuiteError as error:
        raise _fail(f"invalid suite:\n{error}") from None
    _, sha, _ = select_items(loaded, repo)
    source = _checkout(root, repo, sha, checkout)
    if source is None:
        raise _fail(f"no checkout of {repo}: pass --checkout")
    target = out or paths.checkouts_dir(root) / f"{repo}-renamed"
    new_repo = target.name
    if not valid_repo_id(new_repo):
        raise _fail(
            "--out must end in a folder named like a repository id (lowercase letters, digits, "
            f"'.', '_' or '-'), got {new_repo!r}"
        )
    if symbols is not None:
        names: rename_mod.Names = rename_mod.symbols_from_file(symbols)
        names_source = f"file {symbols.name}"
    else:
        names = rename_mod.NAME_SOURCES[names_from](source)
        names_source = names_from.value
    plan = rename_mod.plan_for(source, names, seed=seed)
    try:
        result = rename_mod.rename_tree(source, target, plan)
    except (FileExistsError, ValueError) as error:
        raise _fail(str(error)) from None
    result.sha = rename_mod.commit_tree(target)
    result.repo = new_repo
    result.names = names_source
    origin = RenamedFrom(
        suite=suite_file.stem, repo=repo, sha=sha, sha256=hashlib.sha256(raw).hexdigest()
    )
    result.source = {
        "repo": repo,
        "sha": sha,
        "suite": origin.suite,
        "suite_sha256": origin.sha256,
    }
    try:
        translated = rename_mod.translate_suite(
            loaded, repo, result, new_repo=new_repo, source=origin
        )
    except rename_mod.TranslateError as error:
        raise _fail(f"{error} (the copy at {target} is left for a look; delete it)") from None
    mapping = mapping_path(target.parent, new_repo)  # outside the copy: never indexed
    rename_mod.write_mapping(result, mapping)
    suite_out = suite_out or paths.golden_dir(root) / f"{translated.name}.yaml"
    suite_out.parent.mkdir(parents=True, exist_ok=True)
    suite_out.write_text(rename_mod.suite_yaml(translated), encoding="utf-8")
    typer.echo(
        f"{len(plan.tokens)} names renamed from {names_source} ({len(plan.kept)} kept, listed "
        "in the mapping), "
        f"{result.rewritten} files rewritten, {result.copied} copied; "
        f"{target} ({new_repo}) at {result.sha}\n"
        f"mapping: {mapping}\nsuite: {suite_out} ({len(translated.items)} items)"
    )
