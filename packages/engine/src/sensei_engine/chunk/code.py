"""Code chunks, after cAST: definitions are the units, small neighbours merge, huge ones split at
statement boundaries.

The units come from the file's symbols (:mod:`sensei_engine.symbols.extract`), one level at a
time:

- at the top of the module and inside a class, each definition is a unit, and so is each run of
  other code between them (imports, top-level statements); the comment block right above a
  definition belongs to it, and a run of closing brackets to the definition before it (as does
  a scrap of other code too small to stand alone: ``export default App``);
- a function, method, component or hook is one unit, with everything nested in it;
- a class with methods is its header (the lines down to its first method or nested class:
  decorators, signature, docstring, fields), a chunk of its own, then its members, as the units
  of the next level; a class without methods, or a tiny one, is one unit.

Units under :data:`~sensei_engine.chunk.model.MIN_TOKENS` merge with their small neighbours
(getters, constants, helpers, type aliases), but a class is always a chunk of its own; a unit too
large for one chunk is split before a line where a syntax
node starts, the outermost such boundary that keeps the piece full enough (statements, then the
statements in their blocks), each part's header repeating the signature.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from tree_sitter import Node

from sensei_engine.chunk.header import Header
from sensei_engine.chunk.model import (
    MIN_TOKENS,
    SCRAP_TOKENS,
    Chunk,
    ChunkKind,
    Lines,
    Spec,
    Unit,
    build,
    fold_scraps,
    merge_small,
)
from sensei_engine.parse.languages import COMMENTS, QueryKind
from sensei_engine.parse.parser import ParseResult
from sensei_engine.symbols.extract import Symbol, SymbolKind

# How many classes in a class is split into its members' chunks; one deeper is kept whole in its
# parent's (600 nested Ruby modules would otherwise nest the chunker 1,200 calls deep).
MAX_EXPAND_DEPTH = 32
_CALLABLE = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.COMPONENT, SymbolKind.HOOK}
)
_MEMBER_UNITS = _CALLABLE | {SymbolKind.CLASS, SymbolKind.MODULE}  # what ends a class header
_WORD = re.compile(r"\w")
_RUBY_END = re.compile(r"\bend\b")
_GO_MAJOR = re.compile(r"v\d+")
# Imports that name nothing local (or that the queries capture as a whole statement too), and
# a file's package or namespace declaration.
_UNNAMED_IMPORTS = frozenset(
    {
        "import.wildcard",
        "import.future",
        "import.module",
        "import.dynamic",
        "import.reexport",
        "import.static.wildcard",
        "import.package",
        "import.blank",
        "import.dot",
    }
)
# Languages whose types hold their code (methods, an enum's constructor), every one a class here
# but a type alias; where they don't, only a class is (TypeScript), or a Rust trait or module, a
# Ruby class or module, a C++ class or struct. Go's methods stand outside their types, and C's
# structs hold data only: none is.
_TYPES_HOLD_CODE = frozenset({"java", "kotlin", "csharp", "php"})
_CODE_HOLDERS = {
    "rust": frozenset({"trait", "module"}),
    "ruby": frozenset({None, "module"}),
    "cpp": frozenset({None, "struct"}),
}
_NO_CODE_HOLDERS = frozenset({"go", "c"})


@dataclass(slots=True)
class _Layout:
    cuts: dict[int, int]  # line -> depth of the outermost syntax node that starts it
    comments: set[int]  # lines that hold only a comment


@dataclass(slots=True)
class _Entry:
    unit: Unit
    symbols: list[Symbol]  # none: other code


def chunk_code(result: ParseResult, language: str, symbols: Sequence[Symbol]) -> list[Chunk]:
    """The chunks of a parsed file, in source order; none if it didn't parse (or has no symbols:
    the caller chunks it as text)."""
    if not result.ok or result.tree is None or not symbols:
        return []
    lines = Lines(result.source.decode("utf-8", "replace"))
    if not len(lines):
        return []
    layout = _layout(result.tree.root_node, result.source)
    return _CodeChunker(result, language, lines, layout, symbols).run()


def _layout(root: Node, source: bytes) -> _Layout:
    raw = source.split(b"\n")
    cuts: dict[int, int] = {}
    comments: set[int] = set()
    stack: list[tuple[Node, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        start, end = node.start_point, node.end_point
        starts_line = not raw[start.row][: start.column].strip()
        if node.is_named and depth > 0 and starts_line:
            line = start.row + 1
            cuts[line] = min(depth, cuts.get(line, depth))
        if node.type in COMMENTS:
            row, column = end.row, end.column
            if column == 0 and row > start.row:  # a Rust // comment ends at the next line's start
                row, column = row - 1, len(raw[row - 1])
            if starts_line and row < len(raw) and not raw[row][column:].strip():
                comments.update(range(start.row + 1, row + 2))
            continue
        if end.row > start.row:  # a node on one line starts no other line
            stack.extend((child, depth + 1) for child in node.children)
    return _Layout(cuts, comments)


def imported_names(result: ParseResult) -> list[str]:
    """The local names the file's imports bind, in import order."""
    names: list[str] = []
    for match in result.captures.get(QueryKind.IMPORTS, ()):
        if match.kind in _UNNAMED_IMPORTS:
            continue
        local = (
            match.role("alias")
            or match.role("default")
            or match.role("namespace")
            or match.role("name")
        )
        if local is not None:
            name = result.text(local).rsplit(".", 1)[-1]
        elif match.kind == "import" and (source := match.role("source")) is not None:
            name = _bound(result.text(source), result.language)
        else:
            continue
        if name not in names:
            names.append(name)
    return names


def _bound(source: str, language: str | None) -> str:
    """The name an import without one binds: import os.path binds os; a Go import its path's
    package (json for encoding/json, chi for github.com/go-chi/chi/v5, yaml for
    gopkg.in/yaml.v3)."""
    if language != "go":
        return source.split(".", 1)[0]
    parts = source.split("/")
    name = parts[-2] if len(parts) > 1 and _GO_MAJOR.fullmatch(parts[-1]) else parts[-1]
    return name.split(".", 1)[0]


def _is_class(symbol: Symbol, language: str) -> bool:
    """A class (or a class component): not an interface, a type alias or an enum, which hold no
    code in TypeScript. Java's do (default methods, an enum's constructor and methods), so there
    every type is a class, as a record and an annotation type are; so in Kotlin, C# and PHP.
    Rust's code is in traits and modules, Ruby's in classes and modules, C++'s in classes and
    structs (see :data:`_TYPES_HOLD_CODE`)."""
    subkind = symbol.attrs.get("subkind")
    if language in _TYPES_HOLD_CODE:
        return symbol.kind is SymbolKind.CLASS and subkind != "type"
    if language in _CODE_HOLDERS:
        holds = symbol.kind in (SymbolKind.CLASS, SymbolKind.MODULE)
        return holds and subkind in _CODE_HOLDERS[language]
    if language in _NO_CODE_HOLDERS:
        return False
    return (symbol.kind is SymbolKind.CLASS and subkind is None) or subkind == "class_component"


def _wraps(symbol: Symbol, members: list[Symbol]) -> bool:
    """A C#, PHP or C++ namespace block around its types: a symbol but not their container (they
    are its siblings), so its lines are theirs and the code between them."""
    return symbol.kind is SymbolKind.MODULE and any(
        m is not symbol and symbol.start_line <= m.start_line and m.end_line <= symbol.end_line
        for m in members
    )


class _CodeChunker:
    def __init__(
        self,
        result: ParseResult,
        language: str,
        lines: Lines,
        layout: _Layout,
        symbols: Sequence[Symbol],
    ) -> None:
        self.path = result.path
        self.language = language
        self.lines = lines
        self.layout = layout
        self.module = symbols[0]
        self.by_key = {s.symbol_key: s for s in symbols}
        self.children: dict[str, list[Symbol]] = defaultdict(list)
        for symbol in symbols[1:]:
            if symbol.container_key is not None:
                self.children[symbol.container_key].append(symbol)
        for key, members in self.children.items():
            self.children[key] = [m for m in members if not _wraps(m, members)]
        self.imported = imported_names(result)

    def run(self) -> list[Chunk]:
        return self._level(self.module, 1, len(self.lines), 0)

    # ------------------------------------------------------------------ units

    def _level(self, container: Symbol, start: int, end: int, depth: int) -> list[Chunk]:
        """The chunks of lines ``start``..``end`` inside ``container`` (the module or a class,
        ``depth`` classes in)."""
        chunks: list[Chunk] = []
        run: list[_Entry] = []
        for entry in self._entries(container, start, end):
            expanded = self._expand(entry, depth) if depth < MAX_EXPAND_DEPTH else None
            if expanded is None:
                run.append(entry)
                continue
            chunks.extend(self._merged(container, run))
            run = []
            chunks.extend(expanded)
        chunks.extend(self._merged(container, run))
        return chunks

    def _entries(self, container: Symbol, start: int, end: int) -> list[_Entry]:
        """The definitions inside ``container`` within ``start``..``end``, and the other code
        between them, in order."""
        defined: list[tuple[int, int, list[Symbol]]] = []
        for member in self.children[container.symbol_key]:
            if member.start_line < start or member.end_line > end:
                continue
            if defined and member.start_line <= defined[-1][1]:  # shares lines with the last
                first, last, held = defined[-1]
                defined[-1] = (first, max(last, member.end_line), [*held, member])
                continue
            defined.append((member.start_line, member.end_line, [member]))
        entries: list[_Entry] = []
        position = start
        for begins, last, held in defined:
            first = max(self._with_comments(begins), position)
            if first > position:
                self._other(entries, position, first - 1)
            entries.append(self._entry(first, last, held))
            position = last + 1
        if position <= end:
            self._other(entries, position, end)
        return entries

    def _with_comments(self, line: int) -> int:
        """``line``, or the first line of the comment block right above it."""
        while line > 1 and line - 1 in self.layout.comments:
            line -= 1
        return line

    def _other(self, entries: list[_Entry], start: int, end: int) -> None:
        span = self.lines.trim(start, end)
        if span is None:
            return
        text = self.lines.text(*span)
        if self.language == "ruby":
            text = _RUBY_END.sub("", text)
        if entries and not _WORD.search(text):
            # Closing brackets (Ruby's ends): the end of the definition before (its size counted
            # again).
            before = entries[-1]
            entries[-1] = self._entry(before.unit.start, end, before.symbols)
            return
        entries.append(self._entry(start, end, []))

    def _entry(self, start: int, end: int, symbols: list[Symbol]) -> _Entry:
        tokens = self.lines.tokens(start, end)
        mergeable = tokens < MIN_TOKENS and not any(self._holds_code(s) for s in symbols)
        return _Entry(Unit(start, end, tokens, mergeable), symbols)

    def _expand(self, entry: _Entry, depth: int) -> list[Chunk] | None:
        """A class with methods, as its header and its members' chunks; None for any other
        unit."""
        if len(entry.symbols) != 1 or entry.unit.tokens < MIN_TOKENS:
            return None
        symbol = entry.symbols[0]
        if not self._holds_code(symbol):
            return None  # a function component's handlers are part of it
        members = [
            m
            for m in self.children[symbol.symbol_key]
            if m.kind in _MEMBER_UNITS and m.start_line > symbol.start_line
        ]
        if not members:
            return None
        body = max(self._with_comments(members[0].start_line), entry.unit.start + 1)
        span = (entry.unit.start, body - 1)
        header = build(self.lines, self.path, self._spec([symbol]), span, self.layout.cuts)
        return header + self._level(symbol, body, entry.unit.end, depth + 1)

    def _holds_code(self, symbol: Symbol) -> bool:
        """A class, or a module that holds definitions (not Rust's mod models;)."""
        held = symbol.kind is not SymbolKind.MODULE or bool(self.children[symbol.symbol_key])
        return held and _is_class(symbol, self.language)

    def _merged(self, container: Symbol, run: list[_Entry]) -> list[Chunk]:
        by_unit = {id(entry.unit): entry for entry in run}

        def scrap(units: list[Unit]) -> bool:  # export default App: with its definition
            entries = [by_unit[id(unit)] for unit in units]
            tokens = sum(unit.tokens for unit in units)
            return not any(e.symbols for e in entries) and tokens < SCRAP_TOKENS

        chunks: list[Chunk] = []
        groups = merge_small([entry.unit for entry in run])
        for units, start, end in fold_scraps(groups, scrap):
            entries = [by_unit[id(unit)] for unit in units]
            held = [s for entry in entries for s in entry.symbols]
            if len(entries) == 1 and len(held) == 1:
                spec = self._spec(held)
            else:
                spec = self._other_spec(container, held, start)
            chunks.extend(build(self.lines, self.path, spec, (start, end), self.layout.cuts))
        return chunks

    # ------------------------------------------------------------------ headers

    def _spec(self, symbols: list[Symbol]) -> Spec:
        (symbol,) = symbols
        attrs = symbol.attrs
        exports = None
        if attrs.get("exported"):
            exports = str(attrs.get("export_alias") or symbol.name)
            if attrs.get("default_export"):
                exports = "default" if exports == "default" else f"{exports} (default)"
        header = Header(
            path=self.path,
            language=self.language,
            what=(symbol.kind.value, symbol.qualified_name),  # Store.load: its container too
            exports=exports,
            signature=symbol.signature,
            doc=symbol.docstring,
            decorators=list(attrs.get("decorators", ())),
            imported=self.imported,
        )
        return Spec(
            kind=ChunkKind(symbol.kind.value),
            header=header.render,
            symbol_key=symbol.symbol_key,
            name=symbol.name,
            signature=symbol.signature,
        )

    def _other_spec(self, container: Symbol, held: list[Symbol], start: int) -> Spec:
        """Code that isn't one definition: top-level statements, or several small ones."""
        top = container is self.module
        header = Header(
            path=self.path,
            language=self.language,
            what=("module", container.qualified_name) if top else None,
            container=None if top else container.qualified_name,
            doc=container.docstring if top and start == 1 else None,
            defines=[s.name for s in held],
            imported=self.imported,
        )
        # Its definitions' names go in the path text, as a single definition's name does.
        names = " ".join(s.name for s in held) or None
        return Spec(kind=ChunkKind.CODE, header=header.render, name=names)
