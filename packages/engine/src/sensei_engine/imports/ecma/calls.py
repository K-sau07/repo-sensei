"""What a JavaScript or TypeScript file calls, renders and tests, read from its parse in the
pool (plain data: no tree leaves it). :mod:`sensei_engine.imports.ecma.code` resolves it.

- **Calls** (:class:`CallFact`): the called name, what it is called on (nothing for ``f()``, a
  name or dotted names for ``api.items.list()``, ``this``, ``super``, or an expression), whether
  it is a ``new``, its line and the definition it is made in (the innermost one around it: a
  nested function's key, else the module's).
- **JSX elements** (:class:`JsxFact`): ``<Button>``, ``<Layout.Main>``; intrinsic elements
  (``<div>``, ``<my-element>``) are left out.
- **Callbacks in props** (:class:`CallbackFact`): an attribute given a name or a member
  (``onClick={handleClick}``, ``render={this.renderRow}``), not a call or a literal.
- **Test blocks** (:class:`TestFact`): ``it("…", fn)`` and ``test("…", fn)`` (also ``.only``,
  ``.skip``), with their lines; a call made inside one belongs to it (``CallFact.test``).
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import ClassVar

from tree_sitter import Node

from sensei_engine.imports.ecma.statements import literal
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult
from sensei_engine.symbols.extract import Symbol, SymbolKind

TEST_FUNCTIONS = frozenset({"it", "test"})
_TEST_MODIFIERS = frozenset({"only", "skip", "concurrent", "fails", "todo"})
_DOTTED = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
MAX_TITLE = 200


@dataclass(frozen=True, slots=True)
class CallFact:
    line: int
    name: str  # the called name: f in f(), list in api.items.list()
    receiver: str | None  # None: a plain call; "this", "super", dotted names, or "" (other)
    caller: str  # the key of the definition it is made in (the module's at the top)
    new: bool = False
    test: int | None = None  # the test block it is made in (an index into the file's tests)


@dataclass(frozen=True, slots=True)
class JsxFact:
    line: int
    name: str  # Button, Layout.Main
    caller: str
    test: int | None = None


@dataclass(frozen=True, slots=True)
class CallbackFact:
    line: int
    attribute: str  # onClick
    value: str  # handleClick, this.renderRow, props.onSave
    caller: str


@dataclass(frozen=True, slots=True)
class TestFact:
    __test__: ClassVar[bool] = False  # not a pytest test class

    line: int
    end_line: int
    title: str
    caller: str  # where the block is (the module's key, or a describe's enclosing definition)


@dataclass(frozen=True, slots=True)
class FileCode:
    calls: tuple[CallFact, ...] = ()
    elements: tuple[JsxFact, ...] = ()
    callbacks: tuple[CallbackFact, ...] = ()
    tests: tuple[TestFact, ...] = ()


class _Scopes:
    """The innermost definition around a position (by line and character column)."""

    def __init__(self, result: ParseResult, symbols: Sequence[Symbol]) -> None:
        self._lines = result.source.split(b"\n")
        self.module = next(s.symbol_key for s in symbols if s.kind is SymbolKind.MODULE)
        spans = [s for s in symbols if s.kind is not SymbolKind.MODULE]
        # Smallest last: the innermost one around a position is the last that contains it.
        self._spans = sorted(
            spans,
            key=lambda s: (s.start_line, s.start_col, -s.end_line, -s.end_col),
        )

    def _column(self, row: int, byte_column: int) -> int:
        line = self._lines[row] if row < len(self._lines) else b""
        return len(line[:byte_column].decode("utf-8", "replace"))

    def around(self, node: Node) -> str:
        row, byte_column = node.start_point
        point = (row + 1, self._column(row, byte_column))
        found = self.module
        for span in self._spans:
            if (span.start_line, span.start_col) > point:
                break
            if point < (span.end_line, span.end_col):
                found = span.symbol_key
        return found


def _receiver(result: ParseResult, node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in ("this", "super"):
        return node.type
    text = result.text(node)
    if node.type in ("identifier", "member_expression") and _DOTTED.match(text):
        return text
    return ""


def _test_title(result: ParseResult, call: Node) -> str | None:
    """The title of ``it("…", fn)``/``test.only("…", fn)``; None if ``call`` isn't one."""
    function = call.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "member_expression":
        object_, property_ = (
            function.child_by_field_name("object"),
            function.child_by_field_name("property"),
        )
        if property_ is None or result.text(property_) not in _TEST_MODIFIERS:
            return None
        function = object_
    if function is None or function.type != "identifier":
        return None
    if result.text(function) not in TEST_FUNCTIONS:
        return None
    arguments = call.child_by_field_name("arguments")
    first = arguments.named_children[0] if arguments and arguments.named_children else None
    title = literal(result, first) if first is not None else None
    if title is None or len(arguments.named_children) < 2:  # pyright: ignore[reportOptionalMemberAccess]
        return None
    return title[:MAX_TITLE]


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _callbacks(result: ParseResult, scopes: _Scopes) -> list[CallbackFact]:
    if result.tree is None:
        return []
    found: list[CallbackFact] = []
    for node in _walk(result.tree.root_node):
        if node.type != "jsx_attribute" or len(node.named_children) < 2:
            continue
        name, value = node.named_children[0], node.named_children[-1]
        if value.type != "jsx_expression" or len(value.named_children) != 1:
            continue
        inner = value.named_children[0]
        text = result.text(inner)
        if inner.type in ("identifier", "member_expression") and _DOTTED.match(text):
            found.append(
                CallbackFact(
                    inner.start_point.row + 1, result.text(name), text, scopes.around(node)
                )
            )
    return found


def _is_intrinsic(name: str) -> bool:
    """``div``, ``my-element``, ``svg:rect``: the host's elements, not components."""
    return (name[:1].islower() and "." not in name) or "-" in name or ":" in name


def extract_code(result: ParseResult, symbols: Sequence[Symbol]) -> FileCode:
    """The file's calls, JSX elements, prop callbacks and test blocks (see the module)."""
    if not any(s.kind is SymbolKind.MODULE for s in symbols):
        return FileCode()
    scopes = _Scopes(result, symbols)
    matches = result.captures.get(QueryKind.CALLS, ())
    tests: list[TestFact] = []
    test_spans: list[tuple[int, int, int]] = []  # (start byte, end byte, index)
    for match in matches:
        if match.kind in {"call", "call.method"}:
            title = _test_title(result, match.node)
            if title is not None:
                start, end = match.node.start_point.row + 1, match.node.end_point.row + 1
                test_spans.append((match.node.start_byte, match.node.end_byte, len(tests)))
                tests.append(TestFact(start, end, title, scopes.around(match.node)))
    test_spans.sort()
    starts = [span[0] for span in test_spans]

    def test_of(node: Node) -> int | None:
        """The innermost test block around ``node`` (not the test call itself)."""
        found: int | None = None
        for start, end, index in test_spans[: bisect.bisect_right(starts, node.start_byte)]:
            if start < node.start_byte and node.end_byte <= end:
                found = index
        return found

    calls: list[CallFact] = []
    for match in matches:
        name = match.role("name")
        if name is None:
            continue
        receiver = _receiver(result, match.role("receiver"))
        new = match.kind == "call.new"
        callee = result.text(name)
        if new and name.type == "member_expression":
            receiver, _, callee = callee.rpartition(".")
            receiver = receiver if _DOTTED.match(receiver) else ""
        elif new and name.type != "identifier":
            continue
        calls.append(
            CallFact(
                match.node.start_point.row + 1,
                callee,
                receiver,
                scopes.around(match.node),
                new=new,
                test=test_of(match.node),
            )
        )
    elements: list[JsxFact] = []
    for match in result.captures.get(QueryKind.JSX, ()):
        name = match.role("name")
        if name is None:
            continue
        text = result.text(name)
        if _is_intrinsic(text) or not _DOTTED.match(text):
            continue
        elements.append(
            JsxFact(name.start_point.row + 1, text, scopes.around(match.node), test_of(match.node))
        )
    return FileCode(tuple(calls), tuple(elements), tuple(_callbacks(result, scopes)), tuple(tests))
