"""Checking golden suites against the code at their pinned commits.

A symbol's span runs from its first decorator (or its definition line) to its last line. For
Python the spans come from the standard library's parser, so they are exact (syntax newer than
this interpreter gets approximate spans from indentation, reported as warnings). For TypeScript
and JavaScript, until tree-sitter symbol extraction lands (step 2.4), a declaration is found by
pattern and its end by matching brackets; a span that disagrees with that heuristic is reported
as a warning to re-check by hand, not an error.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sensei_evals import paths
from sensei_evals.repos import PinnedRepo
from sensei_evals.schema import GoldTarget, Suite

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


def read_lines(checkout: Path, path: str) -> list[str]:
    return (checkout / path).read_text(encoding="utf-8", errors="replace").splitlines()


# ---------------------------------------------------------------- python


def _python_symbols(source: str) -> Iterator[SymbolSpan]:
    tree = ast.parse(source)

    def visit(node: ast.AST, prefix: str) -> Iterator[SymbolSpan]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                yield SymbolSpan(name, (start, child.end_lineno or child.lineno), exact=True)
                yield from visit(child, f"{name}.")
            elif isinstance(child, ast.Assign | ast.AnnAssign) and not prefix:
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        end = child.end_lineno or child.lineno
                        yield SymbolSpan(target.id, (child.lineno, end), exact=True)

    yield from visit(tree, "")


# Python 3.14 (PEP 758) allows ``except A, B:``; parenthesized, older parsers read it, and the
# rewrite keeps every line where it was.
_UNPARENTHESIZED_EXCEPT = re.compile(
    r"^(?P<head>[ \t]*except\*?[ \t]+)(?P<types>[^(:\n][^:\n]*,[^:\n]*?)[ \t]*:", re.MULTILINE
)
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
        source = (checkout / path).read_text(encoding="utf-8", errors="replace")
        for attempt in (source, _UNPARENTHESIZED_EXCEPT.sub(r"\g<head>(\g<types>):", source)):
            try:
                return list(_python_symbols(attempt))
            except SyntaxError:
                continue
        # Syntax newer than this interpreter's: approximate spans.
        return list(_python_symbols_by_indent(source.splitlines()))
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


def check_target(checkout: Path, target: GoldTarget) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for one gold target against a checkout."""
    if not (checkout / target.path).is_file():
        return [f"{target.path}: no such file"], []
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


def validate_suite(
    name: str,
    suite: Suite,
    pinned: Mapping[str, PinnedRepo],
    checkout_for: Callable[[str], Path | None],
) -> list[Problem]:
    """Every problem with ``suite``: commits that don't match the pinned ones, checkouts at the
    wrong commit, and gold targets that don't exist at the commit."""
    problems: list[Problem] = []
    checked: dict[str, Path | None] = {}
    for item in suite.items:
        repo = pinned.get(item.repo)
        if repo is None:
            problems.append(Problem(name, item.id, f"repository {item.repo} is not pinned"))
            continue
        if item.sha != repo.sha:
            problems.append(Problem(name, item.id, f"sha {item.sha} is not the pinned {repo.sha}"))
            continue
        if item.repo not in checked:
            checkout = checkout_for(item.repo)
            if checkout is None:
                reason = f"no checkout of {item.repo} to check against"
            elif paths.head_sha(checkout) != repo.sha:
                reason = f"checkout {checkout} is not at {repo.sha}"
                checkout = None
            else:
                reason = ""
            checked[item.repo] = checkout
            if reason:  # said once per repository, on its first item
                problems.append(Problem(name, item.id, reason))
        checkout = checked[item.repo]
        if checkout is None:
            continue
        for target in item.gold:
            errors, warnings = check_target(checkout, target)
            problems += [Problem(name, item.id, message) for message in errors]
            problems += [Problem(name, item.id, message, warning=True) for message in warnings]
    return problems
