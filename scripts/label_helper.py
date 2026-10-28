"""Labeling helper for the golden suites (which stay private, in evals/private/golden/).

    uv run python scripts/label_helper.py show <repo> <path> <start> [end]
    uv run python scripts/label_helper.py symbol <repo> <path> <line>
    uv run python scripts/label_helper.py symbols <repo> <path>
    uv run python scripts/label_helper.py validate [suite.yaml ...] [--fetch]
        [--root DIR] [--checkouts DIR]

``<repo>`` is a pinned repository id (evals/private/repos.yaml), read from its checkout in
.cache/eval-repos/<repo>. ``validate`` checks every suite and CrossLayerBench file (default:
all of them, evals/private/golden/ and evals/private/crosslayerbench/) against the pinned
commits and exits 1 on any error; with no private files (CI) it says so and exits 0. A renamed
copy's suite is also checked against its source suite and the copy's mapping (a stale one says
to re-run ``sensei-eval rename``); a bench's flows are checked on every renamed copy made from
their repository too, as ``sensei-eval run`` derives them. ``--root`` reads the private files and
checkouts of another checkout of this repository; ``--checkouts`` looks for checkouts and
renamed copies (with their mappings) in another folder, such as a ``rename --out`` parent.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from sensei_evals import crosslayer, paths
from sensei_evals.golden import (
    CheckoutError,
    find_symbol,
    read_lines,
    symbol_at,
    symbols_in,
    validate_suite,
)
from sensei_evals.rename import check_renamed
from sensei_evals.repos import fetch_checkout, load_pinned, load_renamed
from sensei_evals.schema import Suite, SuiteError, SuiteProblem, load_suite

ROOT = paths.repo_root(Path(__file__).parent)


def _checkout(repo: str) -> Path:
    checkout = paths.checkouts_dir(ROOT) / repo
    if not checkout.is_dir():
        sys.exit(f"no checkout at {checkout}: run `validate --fetch` or clone the pinned commit")
    return checkout


def show(repo: str, path: str, start: int, end: int | None) -> None:
    lines = read_lines(_checkout(repo), path)
    end = min(end or start + 40, len(lines))
    width = len(str(end))
    for number in range(start, end + 1):
        print(f"{number:>{width}}  {lines[number - 1]}")


def symbol(repo: str, path: str, line: int) -> None:
    found = symbol_at(_checkout(repo), path, line)
    if found is None:
        sys.exit(f"no symbol contains {path}:{line}")
    tag = "" if found.exact else "  (heuristic until tree-sitter symbols)"
    print(f"{found.name}  lines: [{found.lines[0]}, {found.lines[1]}]{tag}")


def symbols(repo: str, path: str, name: str | None) -> None:
    checkout = _checkout(repo)
    found = find_symbol(checkout, path, name) if name else symbols_in(checkout, path)
    for span in found:
        tag = "" if span.exact else "  ~"
        print(f"{span.lines[0]:>5}-{span.lines[1]:<5} {span.name}{tag}")


def _bench_suite(path: Path, checkouts: Path) -> Suite:
    bench, problems = crosslayer.with_renamed(crosslayer.load_bench(path), checkouts)
    if problems:
        raise SuiteError(
            str(path), [SuiteProblem(line=None, where="renamed", message=p) for p in problems]
        )
    return crosslayer.as_suite(bench)


def validate(
    suites: list[Path],
    fetch: bool,
    *,
    root: Path = ROOT,
    checkouts: Path | None = None,
) -> int:
    checkouts = checkouts or paths.checkouts_dir(root)
    # Renamed copies pin themselves; a pinned repository of the same id wins.
    renamed = load_renamed(checkouts)
    pinned = renamed | load_pinned(paths.repos_file(root))
    suites = suites or [
        *sorted(paths.golden_dir(root).glob("*.yaml")),
        *sorted((paths.private_dir(root) / crosslayer.BENCH).glob("*.yaml")),
    ]
    if not suites:
        print("no golden suites (the private files aren't here); skipping")
        return 0

    def checkout_for(repo: str) -> Path | None:
        directory = checkouts / repo
        if fetch and repo in pinned and not pinned[repo].url.startswith("file:"):
            try:
                return fetch_checkout(pinned[repo], directory)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
                raise CheckoutError(f"fetching {repo} into {directory} failed: {error}") from None
        return directory if directory.is_dir() else None

    errors = warnings = items = 0
    for path in suites:
        bench = path.parent.name == crosslayer.BENCH
        try:
            suite = _bench_suite(path, checkouts) if bench else load_suite(path)
        except SuiteError as error:
            print(error)
            errors += len(error.problems)
            continue
        items += len(suite.items)
        problems = validate_suite(path.name, suite, pinned, checkout_for, mix=not bench)
        if not bench:
            problems += check_renamed(path.name, path, suite, checkouts, renamed)
        for problem in problems:
            print(problem.render())
            warnings += problem.warning
            errors += not problem.warning
    print(f"{len(suites)} suites, {items} items: {errors} errors, {warnings} warnings")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("show", help="print a file range with line numbers")
    p.add_argument("repo")
    p.add_argument("path")
    p.add_argument("start", type=int)
    p.add_argument("end", type=int, nargs="?")
    p = commands.add_parser("symbol", help="the symbol (and its span) containing a line")
    p.add_argument("repo")
    p.add_argument("path")
    p.add_argument("line", type=int)
    p = commands.add_parser("symbols", help="symbols in a file, or the spans of one name")
    p.add_argument("repo")
    p.add_argument("path")
    p.add_argument("name", nargs="?")
    p = commands.add_parser("validate", help="check suites against their pinned commits")
    p.add_argument("suites", nargs="*", type=Path)
    p.add_argument("--fetch", action="store_true", help="clone missing pinned checkouts")
    p.add_argument("--root", type=Path, default=ROOT, help="checkout with the private files")
    p.add_argument("--checkouts", type=Path, help="folder of checkouts and renamed copies")
    args = parser.parse_args()
    if args.command == "show":
        show(args.repo, args.path, args.start, args.end)
    elif args.command == "symbol":
        symbol(args.repo, args.path, args.line)
    elif args.command == "symbols":
        symbols(args.repo, args.path, args.name)
    else:
        return validate(args.suites, args.fetch, root=args.root, checkouts=args.checkouts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
