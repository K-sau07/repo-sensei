"""The chunk record, the size budgets, and the line-level splitting and merging every chunker uses.

Sizes are tokens (:mod:`sensei_engine.chunk.tokens`). A unit (a function, a section, a statement)
smaller than :data:`MIN_TOKENS` merges with its small neighbours up to :data:`TARGET_TOKENS`; one
whose header and content would pass :data:`MAX_TOKENS` is split into pieces of about
:data:`TARGET_TOKENS` at the boundaries its chunker allows (statements, paragraphs, keys), each
marked ``part k/n``. No chunk's header and content together are larger than :data:`MAX_TOKENS`.

A chunk's ``content`` is its lines of the file, exactly (line endings normalized, blank lines at
either end dropped); its ``header`` says where it is and what it is. ``search_text`` (keyword
search) is the header and content as index terms (each identifier joined, with its words: see
:func:`~sensei_engine.chunk.identifiers.index_terms`); ``path_text`` the path, signature and
name, the same way. ``content_hash`` covers the header and the content: the same code at the
same place hashes the same in every snapshot.
"""

from __future__ import annotations

import enum
import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from sensei_engine.chunk.identifiers import index_terms
from sensei_engine.chunk.tokens import count_each, count_tokens

MIN_TOKENS = 100
TARGET_TOKENS = 500
MAX_TOKENS = 1000
FULL_ENOUGH = 0.5  # a split piece at least this full may end at a shallower boundary
SCRAP_TOKENS = MIN_TOKENS // 2  # other code this small joins a neighbouring definition


class ChunkKind(enum.StrEnum):
    # Code: one definition (or a part of one).
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"  # a class (with its fields; its methods are chunks of their own)
    COMPONENT = "component"
    HOOK = "hook"
    VARIABLE = "variable"
    MODULE = "module"  # a namespace
    CODE = "code"  # top-level code, or several small definitions together
    # Everything else.
    DOC = "doc"  # Markdown sections
    SQL = "sql"  # statements
    OPENAPI_OPERATION = "openapi_operation"
    CONFIG = "config"  # YAML, JSON and TOML, by key path
    TEXT = "text"  # line windows (other files)


@dataclass(frozen=True, slots=True)
class Chunk:
    kind: ChunkKind
    path: str
    start_line: int  # 1-based, inclusive
    end_line: int
    content: str
    header: str
    search_text: str
    path_text: str
    content_hash: bytes  # SHA-256 of header + content
    tokens: int  # header and content
    symbol_key: str | None = None  # the definition this is (a part of)
    part: tuple[int, int] | None = None  # (k, n) for a split unit
    view: str = "code"


class Lines:
    """A file's lines (1-based) with their token counts."""

    def __init__(self, text: str) -> None:
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        self.lines = [line.removesuffix("\r") for line in lines]
        self._sums = [0]
        for count in count_each([f"{line}\n" for line in self.lines]):
            self._sums.append(self._sums[-1] + count)

    def __len__(self) -> int:
        return len(self.lines)

    def line(self, number: int) -> str:
        return self.lines[number - 1]

    def tokens(self, start: int, end: int) -> int:
        """About how many tokens lines ``start``..``end`` hold (counted line by line)."""
        return self._sums[end] - self._sums[start - 1] if end >= start else 0

    def text(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start - 1 : end])

    def blank(self, number: int) -> bool:
        return not self.lines[number - 1].strip()

    def trim(self, start: int, end: int) -> tuple[int, int] | None:
        """``start``..``end`` without blank lines at either end; None if all blank."""
        while start <= end and self.blank(start):
            start += 1
        while end >= start and self.blank(end):
            end -= 1
        return (start, end) if start <= end else None


def split_lines(
    lines: Lines, start: int, end: int, budget: int, cuts: Mapping[int, int] | None = None
) -> list[tuple[int, int]]:
    """``start``..``end`` in consecutive pieces of at most ``budget`` tokens where possible.

    A piece may end before a line in ``cuts`` (line -> depth: how nested the boundary is; any
    line when None). The piece ends at the shallowest boundary that leaves it at least
    :data:`FULL_ENOUGH` full, else at the furthest one. A line alone over the budget is a piece
    of its own."""
    pieces: list[tuple[int, int]] = []
    while lines.tokens(start, end) > budget and start < end:
        limit = start + 1  # the first line that can't be in this piece
        while limit <= end and lines.tokens(start, limit) <= budget:
            limit += 1
        fitting = [line for line in range(start + 1, limit + 1) if cuts is None or line in cuts]
        if not fitting:  # no boundary fits: as many whole lines as fit, at least one
            cut = max(limit, start + 1)
        else:
            full = [c for c in fitting if lines.tokens(start, c - 1) >= budget * FULL_ENOUGH]
            if full and cuts is not None:
                shallowest = min(cuts[c] for c in full)
                cut = max(c for c in full if cuts[c] == shallowest)
            else:
                cut = fitting[-1]
        pieces.append((start, cut - 1))
        start = cut
    pieces.append((start, end))
    return pieces


@dataclass(slots=True)
class Unit:
    """A run of lines a chunker treats as one thing (a definition, a section, a statement)."""

    start: int
    end: int
    tokens: int
    mergeable: bool  # small enough to share a chunk with small neighbours
    labels: list[str] = field(default_factory=list[str])  # what it holds, for the header


def merge_small(units: Sequence[Unit], target: int = TARGET_TOKENS) -> list[list[Unit]]:
    """Group adjacent mergeable units while the group stays within ``target`` tokens; any other
    unit is a group of its own."""
    groups: list[list[Unit]] = []
    current: list[Unit] = []
    size = 0
    for unit in units:
        joins = (
            unit.mergeable
            and current
            and all(u.mergeable for u in current)
            and size + unit.tokens <= target
        )
        if joins:
            current.append(unit)
            size += unit.tokens
            continue
        if current:
            groups.append(current)
        current, size = [unit], unit.tokens
    if current:
        groups.append(current)
    return groups


def fold_scraps(
    groups: Sequence[list[Unit]], scrap: Callable[[list[Unit]], bool]
) -> list[tuple[list[Unit], int, int]]:
    """``(group, first line, last line)`` for each group, a ``scrap`` group (a few lines too
    small to stand alone) folded into the group before it, or after it when first: its lines
    join that chunk, which is still what that group is."""
    folded: list[tuple[list[Unit], int, int]] = []
    for units in groups:
        if folded and scrap(units):
            main, start, _ = folded[-1]
            folded[-1] = (main, start, units[-1].end)
            continue
        folded.append((units, units[0].start, units[-1].end))
    if len(folded) > 1 and scrap(folded[0][0]):
        main, _, end = folded[1]
        folded[1] = (main, folded[0][1], end)
        folded.pop(0)
    return folded


@dataclass(frozen=True, slots=True)
class Spec:
    """What a chunk is, apart from its lines: the header is built per piece (``uses`` and the
    part change), from ``header(content, part)``."""

    kind: ChunkKind
    header: Callable[[str, tuple[int, int] | None], str]
    symbol_key: str | None = None
    name: str | None = None
    signature: str | None = None


def build(
    lines: Lines,
    path: str,
    spec: Spec,
    span: tuple[int, int],
    cuts: Mapping[int, int] | None = None,
) -> list[Chunk]:
    """The chunks of the lines in ``span``: one, or ``part k/n`` pieces when header and content
    would pass :data:`MAX_TOKENS`."""
    trimmed = lines.trim(*span)
    if trimmed is None:
        return []
    start, end = trimmed
    whole = spec.header(lines.text(start, end), None)
    cap = MAX_TOKENS - count_tokens(whole) - 8  # room for the header, and its part line
    budget = max(MIN_TOKENS, min(TARGET_TOKENS, cap))
    texts = [
        (s, e, text)
        for s, e in _pieces(lines, (start, end), budget, cap, cuts)
        for text in _fit(lines.text(s, e), cap)
    ]
    chunks: list[Chunk] = []
    for index, (s, e, text) in enumerate(texts, 1):
        part = (index, len(texts)) if len(texts) > 1 else None
        header = spec.header(text, part)
        chunks.append(_chunk(path, spec, (s, e), text, header=header, part=part))
    return chunks


def _pieces(
    lines: Lines,
    span: tuple[int, int],
    budget: int,
    cap: int,
    cuts: Mapping[int, int] | None,
) -> list[tuple[int, int]]:
    """Line pieces within ``cap`` tokens (counted exactly), split towards ``budget``."""
    start, end = span
    if count_tokens(lines.text(start, end)) <= cap:
        return [span]
    found: list[tuple[int, int]] = []
    for s, e in split_lines(lines, start, end, budget, cuts):
        piece = lines.trim(s, e)
        if piece is None:
            continue
        if piece[1] > piece[0] and count_tokens(lines.text(*piece)) > cap:
            # The line counts were short: split this piece again, finer.
            found.extend(_pieces(lines, piece, max(1, budget * 4 // 5), cap, cuts))
        else:
            found.append(piece)
    return found


def _fit(text: str, budget: int) -> list[str]:
    """``text`` in as few pieces as keep each within ``budget`` tokens, cut between characters
    (only a single line too long for one chunk needs it: minified code, a data blob)."""
    tokens = count_tokens(text)
    if tokens <= budget:
        return [text]
    count = math.ceil(tokens / budget)
    while True:
        size = math.ceil(len(text) / count)
        pieces = [text[i : i + size] for i in range(0, len(text), size)]
        if all(count_tokens(p) <= budget for p in pieces):
            return pieces
        count += 1


def _chunk(
    path: str,
    spec: Spec,
    span: tuple[int, int],
    content: str,
    *,
    header: str,
    part: tuple[int, int] | None,
) -> Chunk:
    embedded = f"{header}\n{content}"
    return Chunk(
        kind=spec.kind,
        path=path,
        start_line=span[0],
        end_line=span[1],
        content=content,
        header=header,
        search_text=index_terms(embedded),
        path_text=path_text(path, spec.signature, spec.name),
        content_hash=hashlib.sha256(embedded.encode("utf-8", "surrogatepass")).digest(),
        tokens=count_tokens(embedded),
        symbol_key=spec.symbol_key,
        part=part,
    )


def path_text(path: str, signature: str | None, name: str | None) -> str:
    """The path, the signature and the name, as keyword-search terms."""
    return index_terms(" ".join(part for part in (path, signature, name) if part))
