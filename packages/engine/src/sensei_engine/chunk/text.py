"""Chunks of files that aren't parsed code.

- Markdown (and MDX): by heading, each section under its heading path (``Install > Docker``);
- SQL: by statement (sqlglot's tokenizer: ``;`` in strings and ``$$`` bodies don't split);
- OpenAPI (YAML or JSON): by operation (``GET /items/{id}``), the rest as configuration;
- YAML, JSON, TOML: by top-level key path (a TOML table is one key: ``tool.ruff``); a value over
  the target size by its own keys, down to :data:`MAX_KEY_DEPTH` levels;
- anything else: windows of :data:`WINDOW_LINES` lines, overlapping by :data:`WINDOW_OVERLAP`.

Small neighbours (sections, statements, keys) merge, as in code; an operation is always a chunk
of its own. A file its chunker can't read (invalid YAML, an SQL dialect sqlglot doesn't know) is
chunked in windows.
"""

from __future__ import annotations

import bisect
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, cast

import yaml
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokenizer_core import Token, TokenType

from sensei_engine.chunk.header import Header
from sensei_engine.chunk.model import (
    MIN_TOKENS,
    TARGET_TOKENS,
    Chunk,
    ChunkKind,
    Lines,
    Spec,
    Unit,
    build,
    merge_small,
)

WINDOW_LINES = 60
WINDOW_OVERLAP = 10
MAX_KEY_DEPTH = 4
MAX_LABELS = 8
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# PyYAML's stubs leave the stream untyped.
_compose_all = cast("Callable[..., Iterator[yaml.Node | None]]", vars(yaml)["compose_all"])

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_NOT_A_HEADING = re.compile(r"^\s*([-*+>|]|\d+[.)])")
_SQL_LABEL_STOP = frozenset({TokenType.L_PAREN, TokenType.ALIAS, TokenType.SEMICOLON})
_JSON_TOKEN = re.compile(r'"(?:\\.|[^"\\\n])*"|//[^\n]*|/\*.*?\*/|[{}\[\]:,\n]', re.DOTALL)
_TOML_TABLE = re.compile(r"^\s*\[\[?\s*(.+?)\s*\]\]?\s*(?:#.*)?$")
_TOML_NAME = r"""(?:"(?:[^"\\]|\\.)*"|'[^']*'|[\w-]+)"""
_TOML_KEY = re.compile(rf"^\s*({_TOML_NAME})(?:\s*\.\s*{_TOML_NAME})*\s*=")
_TOML_QUOTED = re.compile(r""""(?:[^"\\]|\\.)*"|'[^']*'|#.*""")  # strings and comments


@dataclass(slots=True)
class _Piece:
    unit: Unit
    kind: ChunkKind
    alone: bool = False  # never merged (an OpenAPI operation)
    what: str | None = None  # what its label names, when not the file's usual (operation)
    extra: tuple[tuple[str, str], ...] = ()
    name: str | None = None


def chunk_text(path: str, language: str | None, text: str) -> list[Chunk]:
    """The chunks of a file that isn't chunked as code (``language``: its language id)."""
    lines = Lines(text)
    if not len(lines):
        return []
    found: list[Chunk] = []
    if language in ("markdown", "mdx"):
        found = _markdown(path, language, lines)
    elif language == "sql":
        found = _sql(path, lines, text)
    elif language in ("json", "yaml", "toml"):
        found = _config(path, language, lines, text)
    return found or windows(path, language, lines)


def windows(path: str, language: str | None, lines: Lines) -> list[Chunk]:
    """Overlapping windows of lines (each split further if it is too large for one chunk)."""
    spec = Spec(ChunkKind.TEXT, Header(path, language or "text").render)
    chunks: list[Chunk] = []
    start, count = 1, len(lines)
    while start <= count:
        end = min(start + WINDOW_LINES - 1, count)
        chunks.extend(build(lines, path, spec, (start, end)))
        if end == count:
            break
        start = end - WINDOW_OVERLAP + 1
    return chunks


def _pieces_to_chunks(
    path: str,
    language: str,
    lines: Lines,
    pieces: Sequence[_Piece],
    *,
    what: str,
    cuts: dict[int, int] | None = None,
) -> list[Chunk]:
    """Merge small pieces and build their chunks; ``what`` names a piece's label in headers."""
    chunks: list[Chunk] = []
    by_unit = {id(piece.unit): piece for piece in pieces}
    for piece in pieces:
        piece.unit.mergeable = piece.unit.mergeable and not piece.alone
    for group in merge_small([piece.unit for piece in pieces]):
        held = [by_unit[id(unit)] for unit in group]
        labels = [label for piece in held for label in piece.unit.labels]
        shown = [
            label for piece in held for label in ([piece.name] if piece.name else piece.unit.labels)
        ]
        first = held[0]
        if len(held) == 1:
            named = (first.what or what, labels[0]) if labels else None
            header = Header(path, language, what=named, extra=first.extra)
            name = first.name
        else:
            listed = ", ".join(shown[:MAX_LABELS]) + (" …" if len(shown) > MAX_LABELS else "")
            header = Header(path, language, extra=((f"{what}s", listed),))
            name = None
        spec = Spec(first.kind, header.render, name=name)
        chunks.extend(build(lines, path, spec, (group[0].start, group[-1].end), cuts))
    return chunks


def _unit(lines: Lines, start: int, end: int, label: str | None) -> Unit:
    tokens = lines.tokens(start, end)
    return Unit(start, end, tokens, tokens < MIN_TOKENS, [label] if label else [])


# ---------------------------------------------------------------------- Markdown


def _markdown(path: str, language: str, lines: Lines) -> list[Chunk]:
    count = len(lines)
    starts: list[tuple[int, str]] = [(1, "")]  # (first line, heading path)
    first = 1
    if lines.line(1).strip() == "---":  # front matter
        end = next((n for n in range(2, count + 1) if lines.line(n).strip() in ("---", "...")), 0)
        if end:
            starts = [(1, "front matter")]
            first = end + 1
            if first <= count:
                starts.append((first, ""))
    cuts = _outline(lines, first, starts)
    pieces: list[_Piece] = []
    for index, (start, label) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else count
        if lines.trim(start, end) is None:
            continue
        name = label.rsplit(" > ", 1)[-1] or None
        pieces.append(_Piece(_unit(lines, start, end, label or None), ChunkKind.DOC, name=name))
    return _pieces_to_chunks(path, language, lines, pieces, what="section", cuts=cuts)


def _outline(lines: Lines, first: int, starts: list[tuple[int, str]]) -> dict[int, int]:
    """Add each heading from line ``first`` on to ``starts`` (with its heading path); return
    where a long section may be split (line -> depth: paragraphs before list items before
    code)."""
    headings: list[tuple[int, str]] = []
    cuts: dict[int, int] = {}
    fence: str | None = None
    previous_blank = True
    for number in range(first, len(lines) + 1):
        line = lines.line(number)
        if fence is not None:
            cuts[number] = 3
            if line.strip().startswith(fence):
                fence = None
            continue
        opening = _FENCE.match(line)
        if opening:
            fence = opening.group(1)
            cuts[number] = 1 if previous_blank else 2
            previous_blank = False
            continue
        level, title = _heading(lines, number, previous_blank)
        if level:
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            if starts[-1][0] == number:
                starts.pop()
            starts.append((number, " > ".join(t for _, t in headings)))
            cuts[number] = 0
        elif line.strip():
            cuts[number] = 1 if previous_blank else (2 if _NOT_A_HEADING.match(line) else 4)
        previous_blank = not line.strip()
    return cuts


def _heading(lines: Lines, number: int, previous_blank: bool) -> tuple[int, str]:
    """The heading level and title of a line (0 if it isn't a heading)."""
    line = lines.line(number)
    atx = _ATX.match(line)
    if atx:
        return len(atx.group(1)), (atx.group(2) or "").strip()
    if (
        number < len(lines)
        and previous_blank
        and line.strip()
        and not _NOT_A_HEADING.match(line)
        and (underline := _SETEXT.match(lines.line(number + 1)))
    ):
        return (1 if underline.group(1).startswith("=") else 2), line.strip()
    return 0, ""


# ---------------------------------------------------------------------- SQL


def _sql(path: str, lines: Lines, text: str) -> list[Chunk]:
    try:
        tokens = Dialect.get_or_raise("postgres").tokenize(text)
    except (SqlglotError, ValueError):
        return []
    starts = [0]
    for line in text.split("\n"):
        starts.append(starts[-1] + len(line) + 1)

    def line_of(offset: int) -> int:
        return bisect.bisect_right(starts, offset)

    statements: list[tuple[int, int, str]] = []  # (first line, last line, label)
    current: list[Token] = []
    for token in [*tokens, None]:
        if token is not None and token.token_type is not TokenType.SEMICOLON:
            current.append(token)
            continue
        if current:
            end = token.end if token is not None else current[-1].end
            statements.append((line_of(current[0].start), line_of(end), _sql_label(current)))
        current = []
    pieces: list[_Piece] = []
    position = 1
    for first, last, label in statements:
        if pieces and first <= pieces[-1].unit.end:  # on the line the last one ends on
            unit = pieces[-1].unit
            unit.end = max(unit.end, last)
            unit.tokens = lines.tokens(unit.start, unit.end)
            unit.mergeable = unit.tokens < MIN_TOKENS
            unit.labels.append(label)
        else:
            pieces.append(_Piece(_unit(lines, position, last, label), ChunkKind.SQL))
        position = max(position, last + 1)
    if pieces and position <= len(lines):
        unit = pieces[-1].unit
        unit.end = len(lines)  # trailing comments
    return _pieces_to_chunks(path, "sql", lines, pieces, what="statement")


def _sql_label(tokens: list[Token]) -> str:
    words: list[str] = []
    for token in tokens[:8]:
        if token.token_type in _SQL_LABEL_STOP:
            break
        words.append(token.text)
    return " ".join(words) or tokens[0].text


# ---------------------------------------------------------------------- configuration


@dataclass(frozen=True, slots=True)
class _Key:
    path: tuple[str, ...]
    line: int


def _config(path: str, language: str, lines: Lines, text: str) -> list[Chunk]:
    values: dict[tuple[str, ...], str] = {}
    if language == "toml":
        keys = _toml_keys(lines)
    elif language == "json":
        keys = _json_keys(text)
        try:
            data: Any = json.loads(text)
        except ValueError:
            data = None
        _json_values(data, (), values)
    else:
        keys = _yaml_keys(text, values)
    if not keys:
        return []
    top = {k.path[0] for k in keys if len(k.path) == 1}
    openapi = bool(top & {"openapi", "swagger"}) and "paths" in top
    return _key_chunks(path, language, lines, keys, values, openapi=openapi)


def _key_chunks(
    path: str,
    language: str,
    lines: Lines,
    keys: list[_Key],
    values: dict[tuple[str, ...], str],
    *,
    openapi: bool,
) -> list[Chunk]:
    children: dict[tuple[str, ...], list[_Key]] = defaultdict(list)
    for key in sorted(keys, key=lambda k: k.line):
        children[key.path[:-1]].append(key)
    cuts = {k.line: len(k.path) for k in keys}
    pieces: list[_Piece] = []

    def walk(parent: tuple[str, ...], start: int, end: int) -> None:
        inside = [k for k in children[parent] if start <= k.line <= end]
        if not inside:
            pieces.append(_Piece(_unit(lines, start, end, ".".join(parent)), ChunkKind.CONFIG))
            return
        for index, key in enumerate(inside):
            first = start if index == 0 else key.line
            last = inside[index + 1].line - 1 if index + 1 < len(inside) else end
            if openapi and key.path[0] == "paths" and len(key.path) <= 2:
                walk(key.path, first, last)
            elif (
                openapi
                and len(key.path) == 3
                and key.path[0] == "paths"
                and (key.path[2].lower() in HTTP_METHODS)
            ):
                pieces.append(_operation(lines, key, first, last, values))
            elif (
                lines.tokens(first, last) > TARGET_TOKENS
                and len(key.path) < MAX_KEY_DEPTH
                and children[key.path]
            ):
                walk(key.path, first, last)
            else:
                label = ".".join(key.path)
                pieces.append(_Piece(_unit(lines, first, last, label), ChunkKind.CONFIG))

    walk((), 1, len(lines))
    return _pieces_to_chunks(path, language, lines, pieces, what="key", cuts=cuts)


def _operation(
    lines: Lines, key: _Key, start: int, end: int, values: dict[tuple[str, ...], str]
) -> _Piece:
    _, route, method = key.path
    label = f"{method.upper()} {route}"
    extra = tuple(
        (field, value)
        for field in ("operationId", "summary")
        if (value := values.get((*key.path, field)))
    )
    name = values.get((*key.path, "operationId"))
    return _Piece(
        _unit(lines, start, end, label),
        ChunkKind.OPENAPI_OPERATION,
        alone=True,
        what="operation",
        extra=extra,
        name=name,
    )


def _toml_keys(lines: Lines) -> list[_Key]:
    """Tables as top-level keys (``tool.ruff``), with their keys under them; keys before the
    first table at the top."""
    keys: list[_Key] = []
    table: tuple[str, ...] = ()
    quote: str | None = None  # inside a multi-line string
    depth = 0  # inside a value over several lines: brackets open
    for number in range(1, len(lines) + 1):
        line = lines.line(number)
        if quote is not None:
            if line.count(quote) % 2:
                quote = None
            continue
        if depth > 0:  # an array's rows ([3, 4] is no table)
            depth = max(0, depth + _brackets(line))
            continue
        header = _TOML_TABLE.match(line)
        if header and not _TOML_KEY.match(line):
            table = (header.group(1).replace(" ", ""),)
            keys.append(_Key(table, number))
            continue
        key = _TOML_KEY.match(line)
        if key:
            keys.append(_Key((*table, key.group(1).strip("\"'")), number))
            for delimiter in ('"""', "'''"):
                if line.count(delimiter) % 2:
                    quote = delimiter
            if quote is None:
                depth = max(0, _brackets(line[key.end() :]))
    return keys


def _brackets(text: str) -> int:
    """How many more brackets ``text`` opens than it closes, outside strings and comments."""
    bare = _TOML_QUOTED.sub("", text)
    return bare.count("[") + bare.count("{") - bare.count("]") - bare.count("}")


def _json_keys(text: str) -> list[_Key]:
    """The keys of a JSON (or JSONC) document's objects, down to :data:`MAX_KEY_DEPTH`; none
    when it isn't an object."""
    keys: list[_Key] = []
    stack: list[tuple[str, tuple[str, ...] | None]] = []  # ("{" or "[", the container's path)
    pending: tuple[str, ...] | None = None  # the path of the value that comes next
    expect_key = False
    line = 1
    for match in _JSON_TOKEN.finditer(text):
        lexeme = match.group()
        if lexeme == "\n":
            line += 1
        elif lexeme.startswith(("//", "/*")):
            line += lexeme.count("\n")
        elif lexeme in "{[":
            path: tuple[str, ...] | None = () if not stack else pending
            stack.append((lexeme, path if stack or lexeme == "{" else None))
            expect_key = lexeme == "{"
            pending = None
        elif lexeme in "}]":
            if stack:
                stack.pop()
            expect_key = False
        elif lexeme == ",":
            expect_key = bool(stack) and stack[-1][0] == "{"
        elif lexeme == ":":
            expect_key = False
        elif lexeme.startswith('"'):
            if expect_key and stack and stack[-1][0] == "{":
                parent = stack[-1][1]
                pending = None if parent is None else (*parent, _json_string(lexeme))
                if pending is not None and len(pending) <= MAX_KEY_DEPTH:
                    keys.append(_Key(pending, line))
                expect_key = False
            else:
                pending = None
    return keys


def _json_string(lexeme: str) -> str:
    try:
        return str(json.loads(lexeme))
    except ValueError:
        return lexeme[1:-1]


def _json_values(data: Any, path: tuple[str, ...], values: dict[tuple[str, ...], str]) -> None:
    """Short scalar values by key path (an operation's operationId and summary)."""
    if len(path) > MAX_KEY_DEPTH + 1 or not isinstance(data, dict):
        return
    for key, value in data.items():  # pyright: ignore[reportUnknownVariableType]
        here = (*path, str(key))  # pyright: ignore[reportUnknownArgumentType]
        if isinstance(value, str):
            values[here] = value
        else:
            _json_values(value, here, values)


def _yaml_keys(text: str, values: dict[tuple[str, ...], str]) -> list[_Key]:
    try:
        documents = list(_compose_all(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return []
    keys: list[_Key] = []
    several = len(documents) > 1
    for index, document in enumerate(documents):
        if document is None:
            continue
        root: tuple[str, ...] = ()
        if several:  # each document a key of its own, named by its position
            root = (f"[{index}]",)
            keys.append(_Key(root, document.start_mark.line + 1))
        _yaml_walk(document, root, MAX_KEY_DEPTH + len(root), keys, values)
    return keys


def _yaml_walk(
    node: yaml.Node,
    path: tuple[str, ...],
    depth: int,
    keys: list[_Key],
    values: dict[tuple[str, ...], str],
) -> None:
    if not isinstance(node, yaml.MappingNode):
        return
    pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
    for key_node, value_node in pairs:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        here = (*path, str(key_node.value))
        keys.append(_Key(here, key_node.start_mark.line + 1))
        if isinstance(value_node, yaml.ScalarNode):
            values[here] = str(value_node.value)
        elif len(here) < depth:
            _yaml_walk(value_node, here, depth, keys, values)
