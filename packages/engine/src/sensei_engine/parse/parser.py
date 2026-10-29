"""Parsing one file: its syntax tree, its syntax errors and what the queries capture.

:func:`parse_file` never raises for a file's content. Syntax errors are counted and located
(tree-sitter recovers and keeps parsing around them); a file that takes longer than the timeout,
isn't text, is too large or is in a language we don't parse comes back with that status and no
tree.

The timeout here is checked after parsing and between query kinds: a parse itself can't be
interrupted. tree-sitter's hooks for that are broken in the Python binding on Python 3.13:
its progress callback crashes, and a tree parsed through a read callback keeps the
callback as its source, so ``Node.text``, query predicates and freeing the tree corrupt memory.
The parse stage stops a parse that hangs by killing its worker process
(:mod:`sensei_engine.parse.pool`).

Positions are tree-sitter's: bytes into :attr:`ParseResult.source`, which is the file as UTF-8
(other encodings are converted, a byte order mark is dropped), rows from 0. :func:`lines` gives
a node's 1-based line range.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from tree_sitter import Node, Query, QueryCursor, Tree

from sensei_engine.ingest.classify import MAX_PARSE_BYTES, decode_text
from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.languages import (
    MAIN_CAPTURES,
    Grammar,
    QueryKind,
    grammar_for,
    parser_for,
)

TIMEOUT_SECONDS = 2.0
MAX_ISSUES = 20  # syntax errors located per file (all of them are counted)
_ATTRIBUTE_LISTS = frozenset({"attribute_list", "attribute_group"})  # C#'s [...], PHP's #[...]
_RUST_PREFIX = frozenset({"attribute_item", "line_comment", "block_comment"})
# A decorator's parent that holds what it decorates in a field: Python's decorated definition,
# an exported declaration.
_TARGET_FIELDS = {"decorated_definition": "definition", "export_statement": "declaration"}


class ParseStatus(enum.StrEnum):
    PARSED = "parsed"  # syntax errors or not: see error_count
    TIMED_OUT = "timed_out"
    UNSUPPORTED = "unsupported"  # a language we don't parse
    BINARY = "binary"
    TOO_LARGE = "too_large"
    # Set by the parse stage: the file can't be read, or parsing it raised or crashed.
    UNREADABLE = "unreadable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SyntaxIssue:
    kind: Literal["error", "missing"]  # unparseable text, or a token the parser had to assume
    line: int  # 1-based
    column: int  # 0-based, in bytes
    end_line: int
    end_column: int
    expected: str | None = None  # the missing token or node, for "missing"


@dataclass(frozen=True, slots=True)
class Match:
    """One query match: the main capture (``kind``, ``node``) and its roles."""

    kind: str  # the main capture's name: "definition.function", "import.named", "call"
    pattern: int  # index of the pattern in the grammar's query
    node: Node
    roles: Mapping[str, tuple[Node, ...]]

    def role(self, name: str) -> Node | None:
        nodes = self.roles.get(name)
        return nodes[0] if nodes else None


@dataclass(frozen=True, slots=True)
class ParseResult:
    path: str
    language: str | None  # the grammar's name; None when unsupported
    status: ParseStatus
    source: bytes = b""  # what the tree's positions refer to (UTF-8)
    tree: Tree | None = None
    error_count: int = 0  # ERROR and MISSING nodes
    issues: tuple[SyntaxIssue, ...] = ()  # the first MAX_ISSUES of them, in source order
    captures: Mapping[QueryKind, tuple[Match, ...]] = field(
        default_factory=dict[QueryKind, tuple[Match, ...]]
    )
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """Parsed, with or without syntax errors."""
        return self.status is ParseStatus.PARSED

    def text(self, node: Node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def lines(node: Node) -> tuple[int, int]:
    """The node's first and last line, 1-based (a node ending at the start of a line ends on
    the line before)."""
    start, end = node.start_point.row + 1, node.end_point.row + 1
    if node.end_point.column == 0 and end > start:
        end -= 1
    return start, end


def decorated(decorator: Node) -> Node | None:
    """What a decorator node decorates: the function or class definition (Python); the class,
    method, field or parameter (JavaScript and TypeScript, where a method's decorators precede
    it in the class body); the declaration whose modifiers hold an annotation (Java and Kotlin: a
    type, method, constructor, field declaration, property, parameter or enum constant); the
    declaration an attribute list is on (C#, PHP); the item after a Rust attribute (its
    sibling, other attributes and doc comments maybe between)."""
    if decorator.type == "attribute_item":
        return _next_past(decorator, _RUST_PREFIX)
    parent = decorator.parent
    while parent is not None and parent.type in _ATTRIBUTE_LISTS:
        parent = parent.parent
    if parent is None:
        return None
    if parent.type in _TARGET_FIELDS:
        return parent.child_by_field_name(_TARGET_FIELDS[parent.type])
    if parent.type in ("modifiers", "parameter_modifiers"):
        # Kotlin's parameter annotations stand before the parameter, not in it.
        return parent.parent if parent.type == "modifiers" else parent.next_named_sibling
    if parent.type == "class_body":
        return _next_past(decorator, ("decorator", "comment"))
    return parent


def _next_past(node: Node, skipped: Iterable[str]) -> Node | None:
    """The first named sibling after ``node`` that isn't of a type ``skipped``."""
    skip = frozenset(skipped)
    sibling = node.next_named_sibling
    while sibling is not None and sibling.type in skip:
        sibling = sibling.next_named_sibling
    return sibling


def _source(src: bytes | str) -> bytes | None:
    if isinstance(src, str):
        return src.encode("utf-8", "surrogatepass")
    decoded = decode_text(src)
    return None if decoded is None else decoded[0].encode("utf-8", "surrogatepass")


def _issues(root: Node) -> tuple[int, list[SyntaxIssue]]:
    """Count ERROR and MISSING nodes (an ERROR node counts once, whatever it holds)."""
    count = 0
    issues: list[SyntaxIssue] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            count += 1
            if len(issues) < MAX_ISSUES:
                start, end = node.start_point, node.end_point
                issues.append(
                    SyntaxIssue(
                        kind="missing" if node.is_missing else "error",
                        line=start.row + 1,
                        column=start.column,
                        end_line=end.row + 1,
                        end_column=end.column,
                        expected=node.type if node.is_missing else None,
                    )
                )
            continue
        if node.has_error:
            stack.extend(reversed(node.children))
    return count, issues


def _main_capture(kind: QueryKind, names: Iterable[str]) -> str | None:
    roots = MAIN_CAPTURES[kind]
    found = sorted(n for n in names if any(n == r or n.startswith(f"{r}.") for r in roots))
    return found[0] if found else None


def run_query(kind: QueryKind, query: Query, root: Node) -> tuple[Match, ...]:
    """The query's matches under ``root``, one per fact, in source order (outer nodes first,
    then by where their roles are).

    A node captured by several patterns with the same roles is kept once, with the first
    pattern's kind (the query files list specific patterns first)."""
    kept: dict[tuple[int, tuple[tuple[str, int], ...]], Match] = {}
    for pattern, captured in QueryCursor(query).matches(root):
        main = _main_capture(kind, captured)
        if main is None:
            continue
        node = captured[main][0]
        roles = {
            name: tuple(nodes)
            for name, nodes in captured.items()
            if name != main and not name.startswith("_")
        }
        key = (node.id, tuple(sorted((r, n.id) for r, nodes in roles.items() for n in nodes)))
        known = kept.get(key)
        if known is None or pattern < known.pattern:
            kept[key] = Match(kind=main, pattern=pattern, node=node, roles=roles)
    return tuple(sorted(kept.values(), key=_position))


def _position(match: Match) -> tuple[int, int, tuple[int, ...], int]:
    """Source order: by node (outer first), then by the roles (import a, b: a first)."""
    roles = tuple(sorted(n.start_byte for nodes in match.roles.values() for n in nodes))
    return (match.node.start_byte, -match.node.end_byte, roles, match.pattern)


def parse_file(
    path: str,
    src: bytes | str,
    *,
    language: str | None = None,
    kinds: Iterable[QueryKind] | None = None,
    timeout_seconds: float = TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ParseResult:
    """Parse ``src`` (the file at ``path``, relative POSIX) and run the queries ``kinds`` (all
    by default). ``language`` is the file's language id; detected from the path when None."""
    started = clock()
    if language is None:
        head = src[:256] if isinstance(src, bytes) else src[:256].encode("utf-8", "replace")
        language = detect_language(path, head)
    found = grammar_for(language)
    if found is None:
        return ParseResult(path=path, language=None, status=ParseStatus.UNSUPPORTED)
    size = len(src) if isinstance(src, bytes) else len(src.encode("utf-8", "surrogatepass"))
    if size > MAX_PARSE_BYTES:
        return ParseResult(path=path, language=found.name, status=ParseStatus.TOO_LARGE)
    source = _source(src)
    if source is None:
        return ParseResult(path=path, language=found.name, status=ParseStatus.BINARY)
    deadline = started + timeout_seconds
    tree = _parse(found, source)
    if clock() > deadline:
        return _timed_out(path, found, source, started, clock)
    root = tree.root_node
    error_count, issues = _issues(root) if root.has_error else (0, [])
    captures: dict[QueryKind, tuple[Match, ...]] = {}
    for kind in QueryKind if kinds is None else kinds:
        query = found.queries.get(kind)
        captures[kind] = run_query(kind, query, root) if query is not None else ()
        if clock() > deadline:
            return _timed_out(path, found, source, started, clock)
    return ParseResult(
        path=path,
        language=found.name,
        status=ParseStatus.PARSED,
        source=source,
        tree=tree,
        error_count=error_count,
        issues=tuple(issues),
        captures=captures,
        seconds=clock() - started,
    )


def _parse(found: Grammar, source: bytes) -> Tree:
    return parser_for(found).parse(source)


def _timed_out(
    path: str, found: Grammar, source: bytes, started: float, clock: Callable[[], float]
) -> ParseResult:
    return ParseResult(
        path=path,
        language=found.name,
        status=ParseStatus.TIMED_OUT,
        source=source,
        seconds=clock() - started,
    )
