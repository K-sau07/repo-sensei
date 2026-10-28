"""Checking golden suites against the code at their pinned commits.

A symbol's span runs from its first decorator (or its definition line) to its last line. For
Python the spans come from the standard library's parser, so they are exact (syntax newer than
this interpreter gets approximate spans from indentation, reported as warnings). For TypeScript
and JavaScript, until tree-sitter symbol extraction lands (step 2.4), a declaration is found by
pattern and its end by matching brackets; a span that disagrees with that heuristic is reported
as a warning to re-check by hand, not an error.

Line numbers count ``\\n`` only (see :func:`split_lines`), and a gold target must be a file the
indexer keeps (see :func:`indexed_paths`): retrieval can never return anything else.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sensei_engine.ingest.ignore import IgnoreRules
from sensei_engine.ingest.walk import walk
from sensei_evals import paths
from sensei_evals.repos import PinnedRepo
from sensei_evals.schema import GoldTarget, QueryClass, Suite

MAX_FILES = 500_000
TS_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_TS_DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\*?\s+(?P<function>[A-Za-z_$][\w$]*)"
    r"|class\s+(?P<class>[A-Za-z_$][\w$]*)"
    r"|(?:interface|type|enum)\s+(?P<type>[A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+(?P<variable>[A-Za-z_$][\w$]*))"
)
_TS_METHOD = re.compile(
    r"^\s+(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*[<(]"
)
_NOT_METHODS = frozenset({"if", "for", "while", "switch", "return", "catch", "function", "super"})


@dataclass(frozen=True, slots=True)
class SymbolSpan:
    name: str  # qualified for Python (Class.method), plain otherwise
    lines: tuple[int, int]
    exact: bool  # from a real parser, not the TypeScript heuristic


@dataclass(frozen=True, slots=True)
class Problem:
    suite: str
    item: str
    message: str
    warning: bool = False

    def render(self) -> str:
        kind = "warning" if self.warning else "error"
        return f"{self.suite}: {self.item}: {kind}: {self.message}"


def split_lines(text: str) -> list[str]:
    """``text`` cut at ``\\n`` only, the way git, the indexer and editors number lines.
    ``str.splitlines`` also cuts at ``\\f``, ``\\v``, ``\\x1c``-``\\x1e``, ``\\x85``, U+2028,
    U+2029 and a lone ``\\r``, which would shift every later line. A final newline ends the last
    line rather than starting an empty one, and the ``\\r`` of a ``\\r\\n`` is dropped."""
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def read_text(checkout: Path, path: str) -> str:
    """A file's text as stored (no newline translation), undecodable bytes replaced."""
    return (checkout / path).read_bytes().decode("utf-8", errors="replace")


def read_lines(checkout: Path, path: str) -> list[str]:
    return split_lines(read_text(checkout, path))


def indexed_paths(checkout: Path) -> frozenset[str]:
    """The files indexing keeps in ``checkout``: the ingest walk with no repository settings, so
    ``.gitignore``/``.senseiignore`` rules and the built-in deny list apply, and symbolic links
    and ``.git`` are left out."""
    files, _ = walk(checkout, IgnoreRules(()), max_files=MAX_FILES)
    return frozenset(file.path for file in files)


# ---------------------------------------------------------------- python


def _blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists of a compound statement that runs where it stands, so what it
    defines belongs to the enclosing module or class: ``if TYPE_CHECKING:`` blocks,
    ``try``/``except ImportError`` fallbacks (every handler, ``else`` and ``finally``),
    ``with`` and ``match``."""
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, ast.Try | ast.TryStar):
        return [
            node.body,
            *(handler.body for handler in node.handlers),
            node.orelse,
            node.finalbody,
        ]
    if isinstance(node, ast.With | ast.AsyncWith):
        return [node.body]
    if isinstance(node, ast.Match):
        return [case.body for case in node.cases]
    return []


def _python_symbols(tree: ast.Module) -> Iterator[SymbolSpan]:
    def visit(body: list[ast.stmt], prefix: str, *, namespace: bool) -> Iterator[SymbolSpan]:
        """The definitions in ``body``. In a module or class body (``namespace``) variables are
        symbols too (``NAME``, ``Class.attr``), and blocks that run in place are entered."""
        for child in body:
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                yield SymbolSpan(name, (start, child.end_lineno or child.lineno), exact=True)
                is_class = isinstance(child, ast.ClassDef)
                yield from visit(child.body, f"{name}.", namespace=is_class)
            elif not namespace:
                continue
            elif isinstance(child, ast.Assign | ast.AnnAssign):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                end = child.end_lineno or child.lineno
                for target in targets:
                    if isinstance(target, ast.Name):
                        yield SymbolSpan(f"{prefix}{target.id}", (child.lineno, end), exact=True)
            else:
                for block in _blocks(child):
                    yield from visit(block, prefix, namespace=True)

    yield from visit(tree.body, "", namespace=True)


# Python 3.14 (PEP 758) allows ``except A, B:``; parenthesized, older parsers read it, and the
# rewrite keeps every line where it was.
_UNPARENTHESIZED_EXCEPT = re.compile(
    r"^(?P<head>[ \t]*except\*?[ \t]+)(?P<types>[^(:\n][^:\n]*,[^:\n]*?)[ \t]*:", re.MULTILINE
)
_LONE_CR = re.compile(r"\r(?!\n)")


def parse_python(source: str) -> ast.Module | None:
    """The module's tree (reading Python 3.14's unparenthesized ``except A, B:`` too, on the
    same lines), or None if this interpreter can't parse it. Python ends a line at a lone
    ``\\r`` too; here it reads as a space, so the tree's line numbers are :func:`split_lines`'s
    and its columns stay where they were."""
    source = _LONE_CR.sub(" ", source)
    for attempt in (source, _UNPARENTHESIZED_EXCEPT.sub(r"\g<head>(\g<types>):", source)):
        try:
            return ast.parse(attempt)
        except (SyntaxError, ValueError):  # ValueError: a null byte
            continue
    return None


_PY_DEF = re.compile(r"^(?P<indent>[ \t]*)(?:async[ \t]+)?(?:def|class)[ \t]+(?P<name>\w+)")
_PY_ASSIGN = re.compile(r"^(?P<name>[A-Za-z_]\w*)[ \t]*(?::[^=]+)?=(?!=)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


_PY_STRING = re.compile(
    r"""(?:[rbfuRBFU]{0,2})(?:"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*')"""
)


def _depth(line: str) -> int:
    code = _PY_STRING.sub('""', line).split("#", 1)[0]
    return sum(code.count(c) for c in "([{") - sum(code.count(c) for c in ")]}")


def _mask_strings(lines: list[str]) -> list[str]:
    """``lines`` with every line that starts inside a triple-quoted string blanked, so string
    text at any indentation reads as part of the code around it (marked by a lone ``#``)."""
    masked: list[str] = []
    quote: str | None = None
    for line in lines:
        masked.append("#" if quote else line)
        code = _PY_STRING.sub('""', line) if quote is None else line
        i = 0
        while i < len(code):
            if quote is None and code[i] == "#":
                break
            for mark in ('"""', "'''"):
                if code.startswith(mark, i) and quote in {None, mark}:
                    quote = None if quote else mark
                    i += 2
                    break
            i += 1
    return masked


def _bracket_end(lines: list[str], number: int, *, until_colon: bool = False) -> int:
    """The line where the statement starting on line ``number`` closes its brackets (and, for a
    ``def`` or ``class`` header, reaches its colon)."""
    end, depth = number, _depth(lines[number - 1])

    def open_(line: int) -> bool:
        code = lines[line - 1].split("#", 1)[0].rstrip()
        return depth > 0 or (until_colon and not code.endswith(":"))

    while open_(end) and end < len(lines):
        end += 1
        depth += _depth(lines[end - 1])
    return end


def _decorator_start(lines: list[str], number: int, indent: int) -> int:
    """The first decorator line above the definition on line ``number`` (multi-line ones too)."""
    start = number
    for above in range(number - 1, 0, -1):
        text = lines[above - 1]
        if not text.strip():
            break
        if _indent(text) == indent and text.lstrip().startswith("@"):
            start = above
        elif not (_indent(text) > indent or text.lstrip().startswith((")", "]"))):
            break
    return start


def _block_end(lines: list[str], header: int, indent: int) -> int:
    """The last code line indented under the header ending on line ``header``."""
    end = header
    for below in range(header + 1, len(lines) + 1):
        text = lines[below - 1]
        if text.lstrip().startswith("#"):  # a comment, or "#" for masked string text
            end = below if text == "#" else end
            continue
        if text.strip():
            if _indent(text) <= indent:
                break
            end = below
    return end


def _python_symbols_by_indent(source: list[str]) -> Iterator[SymbolSpan]:
    """Spans from indentation, for code this interpreter can't parse (newer syntax): a
    definition runs from its first decorator to the last line indented under it."""
    lines = _mask_strings(source)
    stack: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        match = _PY_DEF.match(line)
        if match is None:
            if assign := _PY_ASSIGN.match(line):
                yield SymbolSpan(assign.group("name"), (number, _bracket_end(lines, number)), False)
            continue
        indent = _indent(line)
        header = _bracket_end(lines, number, until_colon=True)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        name = ".".join([*(n for _, n in stack), match.group("name")])
        stack.append((indent, name))
        span = (_decorator_start(lines, number, indent), _block_end(lines, header, indent))
        yield SymbolSpan(name, span, exact=False)


# ---------------------------------------------------------------- typescript (heuristic)


_CONTINUES = ("=", "(", "{", "[", ",", "|", "&", "?", ":", "<", "+", "-", "=>", ".")


def _continues(lines: list[str], number: int) -> bool:
    """Whether the statement on line ``number`` goes on to the next line (semicolon-free code:
    a trailing operator, or a next line that starts a chained call)."""
    if lines[number - 1].rstrip().endswith(_CONTINUES):
        return True
    following = lines[number].lstrip() if number < len(lines) else ""
    return following.startswith((".", "?.", "|", "&", ">", "satisfies ", "as "))


def _statement_end(lines: list[str], start: int) -> int:
    """The 1-based last line of the statement starting on line ``start``: where its brackets
    are all closed and nothing continues it. Strings, template literals and line comments are
    skipped roughly (good enough for labeling)."""
    depth = 0
    quote: str | None = None
    for number in range(start, len(lines) + 1):
        line = lines[number - 1]
        i = 0
        while i < len(line):
            char = line[i]
            if quote:
                if char == "\\":
                    i += 1
                elif char == quote:
                    quote = None
            elif char in "\"'`":
                quote = char
            elif line.startswith("//", i):
                break
            elif char in "{([":
                depth += 1
            elif char in "})]":
                depth -= 1
            i += 1
        if quote in {'"', "'"}:
            quote = None  # those quotes don't span lines
        if depth <= 0 and quote is None and not _continues(lines, number):
            return number
    return len(lines)


def _ts_symbols(lines: list[str]) -> Iterator[SymbolSpan]:
    for number, line in enumerate(lines, start=1):
        match = _TS_DECLARATION.match(line)
        if match is None or line.startswith((" ", "\t")):
            continue  # top-level declarations only
        name = next(group for group in match.groups() if group)
        end = _statement_end(lines, number)
        yield SymbolSpan(name, (number, end), exact=False)
        if match.group("class"):
            yield from _ts_methods(lines, name, number, end)


def _ts_methods(lines: list[str], owner: str, start: int, end: int) -> Iterator[SymbolSpan]:
    """``Class.method`` spans for the methods declared directly in a class body."""
    body_indent: int | None = None
    number = start + 1
    while number < end:
        line = lines[number - 1]
        indent = len(line) - len(line.lstrip())
        match = _TS_METHOD.match(line)
        if line.strip() and body_indent is None and not line.lstrip().startswith(("/*", "*", "//")):
            body_indent = indent
        if match and indent == body_indent and match.group("name") not in _NOT_METHODS:
            last = _statement_end(lines, number)
            yield SymbolSpan(f"{owner}.{match.group('name')}", (number, last), exact=False)
            number = last
        number += 1


# ---------------------------------------------------------------- lookups


def symbols_in(checkout: Path, path: str) -> list[SymbolSpan]:
    """Every symbol the labeling tools know in one file (empty for other languages)."""
    suffix = PurePosixPath(path).suffix
    if suffix == ".py":
        source = read_text(checkout, path)
        tree = parse_python(source)
        if tree is not None:
            return list(_python_symbols(tree))
        # Syntax newer than this interpreter's: approximate spans.
        return list(_python_symbols_by_indent(split_lines(source)))
    if suffix in TS_SUFFIXES:
        return list(_ts_symbols(read_lines(checkout, path)))
    return []


def symbol_at(checkout: Path, path: str, line: int) -> SymbolSpan | None:
    """The innermost symbol whose span contains ``line``."""
    containing = [s for s in symbols_in(checkout, path) if s.lines[0] <= line <= s.lines[1]]
    return min(containing, key=lambda s: s.lines[1] - s.lines[0], default=None)


def find_symbol(checkout: Path, path: str, name: str) -> list[SymbolSpan]:
    return [s for s in symbols_in(checkout, path) if s.name == name]


# ---------------------------------------------------------------- validation


class CheckoutError(Exception):
    """A checkout that couldn't be had (a failed fetch): reported against its repository."""


def check_target(
    checkout: Path, target: GoldTarget, indexed: frozenset[str] | None = None
) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for one gold target against a checkout. With ``indexed`` (the
    checkout's :func:`indexed_paths`), a target the indexer skips is an error too."""
    if not (checkout / target.path).is_file():
        return [f"{target.path}: no such file"], []
    if indexed is not None and target.path not in indexed:
        reason = "ignore rules or the built-in deny list skip it, or it is behind a symbolic link"
        return [f"{target.path}: not a file the indexer keeps ({reason})"], []
    if target.lines is None:
        return [], []
    total = len(read_lines(checkout, target.path))
    start, end = target.lines
    if end > total:
        return [f"{target.path}: lines {start}-{end} run past the end (line {total})"], []
    if target.symbol is None:
        return [], []
    return _check_symbol(checkout, target.path, target.symbol, target.lines)


def _check_symbol(
    checkout: Path, path: str, symbol: str, lines: tuple[int, int]
) -> tuple[list[str], list[str]]:
    start, end = lines
    found = find_symbol(checkout, path, symbol)
    if not found:
        message = f"{path}: symbol {symbol} not found"
        if PurePosixPath(path).suffix == ".py":
            return [message], []
        # Heuristic languages miss some declaration forms: the name must at least be there.
        span = "\n".join(read_lines(checkout, path)[start - 1 : end])
        if not re.search(
            rf"(?<![\w$]){re.escape(symbol.rsplit('.', maxsplit=1)[-1])}(?![\w$])", span
        ):
            return [f"{message}, and it isn't in lines {start}-{end}"], []
        return [], [f"{message} by the heuristic; check lines {start}-{end} by hand"]
    if any(span.lines == lines for span in found):
        return [], []
    spans = ", ".join(f"{s.lines[0]}-{s.lines[1]}" for s in found)
    message = f"{path}: {symbol} is at lines {spans}, the gold says {start}-{end}"
    return ([message], []) if all(span.exact for span in found) else ([], [message])


# The 2.2 mix per repository: answerable items by class, and at least this many unanswerable.
CLASS_MIX: dict[QueryClass, int] = {
    QueryClass.CONCEPTUAL: 10,
    QueryClass.FLOW: 10,
    QueryClass.SYMBOL: 5,
    QueryClass.REGEX: 3,
}
MIN_UNANSWERABLE = 2


def check_mix(name: str, suite: Suite) -> list[Problem]:
    """A warning for each repository whose items depart from the planned class mix."""
    problems: list[Problem] = []
    for repo in suite.repos:
        counts = Counter(item.class_ for item in suite.for_repo(repo))
        unanswerable = counts.pop(QueryClass.UNANSWERABLE, 0)
        off = [
            f"{cls.value} {counts.get(cls, 0)} (planned {CLASS_MIX.get(cls, 0)})"
            for cls in QueryClass
            if cls is not QueryClass.UNANSWERABLE and counts.get(cls, 0) != CLASS_MIX.get(cls, 0)
        ]
        if unanswerable < MIN_UNANSWERABLE:
            off.append(f"unanswerable {unanswerable} (planned at least {MIN_UNANSWERABLE})")
        if off:
            message = f"class mix departs from the plan: {', '.join(off)}"
            problems.append(Problem(name, f"repo {repo}", message, warning=True))
    return problems


def _checkout_problem(checkout: Path, repo: PinnedRepo) -> str:
    """Why ``checkout`` can't vouch for ``repo``'s pinned commit ("" if it can)."""
    if paths.head_sha(checkout) != repo.sha:
        return f"checkout {checkout} is not at {repo.sha}"
    if paths.has_uncommitted_changes(checkout):
        return f"checkout {checkout} has local changes (git status isn't clean)"
    return ""


def validate_suite(
    name: str,
    suite: Suite,
    pinned: Mapping[str, PinnedRepo],
    checkout_for: Callable[[str], Path | None],
    *,
    mix: bool = True,
) -> list[Problem]:
    """Every problem with ``suite``: commits that don't match the pinned ones, checkouts at the
    wrong commit or with local changes, gold targets that don't exist at the commit (or that the
    indexer skips), and, as warnings, a class mix other than the plan's (unless not ``mix``: a
    suite of one class). ``checkout_for`` may raise :class:`CheckoutError`, reported once for
    that repository."""
    problems: list[Problem] = check_mix(name, suite) if mix else []
    checked: dict[str, tuple[Path, frozenset[str]] | None] = {}
    for item in suite.items:
        repo = pinned.get(item.repo)
        if repo is None:
            problems.append(Problem(name, item.id, f"repository {item.repo} is not pinned"))
            continue
        if item.sha != repo.sha:
            problems.append(Problem(name, item.id, f"sha {item.sha} is not the pinned {repo.sha}"))
            continue
        if item.repo not in checked:
            try:
                checkout = checkout_for(item.repo)
            except CheckoutError as error:
                checkout, reason = None, str(error)
            else:
                reason = (
                    f"no checkout of {item.repo} to check against"
                    if checkout is None
                    else _checkout_problem(checkout, repo)
                )
            if checkout is None or reason:
                checked[item.repo] = None
            else:
                checked[item.repo] = (checkout, indexed_paths(checkout))
            if reason:  # said once per repository, on its first item
                problems.append(Problem(name, item.id, reason))
        entry = checked[item.repo]
        if entry is None:
            continue
        checkout, indexed = entry
        for target in item.gold:
            errors, warnings = check_target(checkout, target, indexed)
            problems += [Problem(name, item.id, message) for message in errors]
            problems += [Problem(name, item.id, message, warning=True) for message in warnings]
    return problems
