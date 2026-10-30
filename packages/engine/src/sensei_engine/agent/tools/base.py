"""What every agent tool shares (4.1): the snapshot it reads (:class:`ToolContext`), the answer
it gives (:class:`ToolResult`), and the shape of that answer.

Code comes back as Anthropic ``search_result`` blocks with citations on, so the model cites
lines rather than retelling them: ``source`` is ``<repo>@<sha>:<path>#L<a>-L<b>``, ``title`` the
symbol or the path, and ``content`` text blocks of 5 to 20 lines, each line after its number
(``12\\tdef create_item(...)``). A block is the smallest thing a citation can point at, so the
:class:`CitationIndex` keeps each block's lines: a citation of blocks 1 to 3 of the 4th search
result of the run is those lines of that file, at that commit.

Every result fits in about :data:`MAX_RESULT_TOKENS`: what doesn't is left out with a note of
how much was, and of how to narrow the call. A list longer than a page ends with a
``next_cursor``. An error says what was wrong and gives a call that works
(:class:`ToolError`)."""

from __future__ import annotations

import base64
import hashlib
import json
from bisect import bisect_left
from collections.abc import Awaitable, Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from sensei_core.models import CodeNode, RepoFile
from sensei_engine.chunk.tokens import count_each, count_tokens
from sensei_engine.ingest.classify import decode_text, text_lines
from sensei_engine.search.grep import ExactSearch
from sensei_engine.search.service import SearchService
from sensei_engine.snapshots import UnsafePathError, check_path, read_file
from sensei_engine.symbols.extract import SymbolKind

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sensei_engine.graph.snapshot import GraphSnapshot
    from sensei_engine.search.hybrid import DenseLegs, GrepLegs

MAX_RESULT_TOKENS = 6_000  # a tool result's text, at most (cl100k tokens, near Claude's count)
NOTE_TOKENS = 200  # kept back for the notes: a page's (its cursor), what was left out
BLOCK_LINES = 12  # a search result's text blocks: this many lines, the last 5 to 20
MIN_BLOCK_LINES = 5
MAX_LINE_CHARS = 400  # a longer line is cut (minified code), with an ellipsis
MAX_FILE_BYTES = 2 * 1024 * 1024  # a file read past this is too large to read here
SYMBOL_KINDS = frozenset(kind.value for kind in SymbolKind)
LANES = ("ui", "api", "svc", "db", "other")  # the layers a search or evidence names


@dataclass(frozen=True, slots=True)
class Evidence:
    """A piece of code the agent commits to answering from (``submit_evidence``)."""

    target: str  # the symbol's readable id, or the path
    path: str
    start_line: int
    end_line: int
    layer: str
    why: str


@dataclass(slots=True)
class ToolContext:
    """One indexed snapshot, as the tools read it: its rows (``db``, under the org's RLS), its
    files unpacked at ``root``, and its graph (None: not built yet)."""

    db: AsyncSession
    org_id: UUID
    repo_id: UUID
    snapshot_id: UUID
    repo: str  # owner/name, as a citation's source names it
    sha: str  # the commit
    root: Path
    tree: Sequence[str]  # every file the snapshot records, sorted
    searchable: Container[str] | None = None  # the files grep may report (None: every one)
    search: SearchService = field(default_factory=SearchService)
    exact: ExactSearch = field(default_factory=ExactSearch)
    graph: GraphSnapshot | None = None
    grep_legs: GrepLegs | None = None  # code_search's exact legs (None: grep on ``root``)
    dense: DenseLegs | None = None  # its semantic legs (None: embeddings not ready)
    evidence: list[Evidence] = field(default_factory=list[Evidence])

    def source(self, path: str, start_line: int, end_line: int) -> str:
        return f"{self.repo}@{self.sha}:{path}#L{start_line}-L{end_line}"


class ToolError(Exception):
    """A call the tool can't answer, said so the model can fix it: what was wrong, and a call
    that works."""

    def __init__(self, message: str, *, example: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.example = example

    def text(self) -> str:
        return f"{self.message}\nFor example: {self.example}" if self.example else self.message


@dataclass(frozen=True, slots=True)
class Source:
    """What one ``search_result`` block cites: a file's lines at the commit, block by block."""

    path: str
    sha: str
    blocks: tuple[tuple[int, int], ...]  # each text block's first and last line
    symbol_key: str | None = None

    @property
    def start_line(self) -> int:
        return self.blocks[0][0]

    @property
    def end_line(self) -> int:
        return self.blocks[-1][1]


@dataclass(frozen=True, slots=True)
class Cited:
    """The lines a citation points at."""

    path: str
    sha: str
    start_line: int
    end_line: int
    symbol_key: str | None


class CitationIndex:
    """The run's search results in the order the model saw them (across messages and tool
    results, as the API counts ``search_result_index``), and the lines each block holds."""

    def __init__(self) -> None:
        self._sources: list[Source] = []

    def __len__(self) -> int:
        return len(self._sources)

    def add(self, sources: Iterable[Source]) -> None:
        self._sources.extend(sources)

    @property
    def sources(self) -> tuple[Source, ...]:
        return tuple(self._sources)

    def resolve(self, result_index: int, start_block: int, end_block: int) -> Cited | None:
        """The lines of blocks ``start_block`` up to ``end_block`` (exclusive) of the result,
        or None for a citation that points at nothing the run gave."""
        if not 0 <= result_index < len(self._sources):
            return None
        source = self._sources[result_index]
        blocks = source.blocks[start_block:end_block] if start_block >= 0 else ()
        if not blocks:
            return None
        return Cited(source.path, source.sha, blocks[0][0], blocks[-1][1], source.symbol_key)


@dataclass(slots=True)
class ToolResult:
    """A tool's answer: its content blocks (text, ``search_result``), the source of each
    ``search_result`` among them in order, and a line for the run's events."""

    content: list[dict[str, Any]]
    sources: list[Source]
    summary: str
    hits: int = 0
    is_error: bool = False

    def param(self, tool_use_id: str) -> dict[str, Any]:
        """The ``tool_result`` block that answers the call ``tool_use_id``."""
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": self.content,
        }
        if self.is_error:
            block["is_error"] = True
        return block


Run = Callable[[ToolContext, Mapping[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class Tool:
    """A tool as the model is given it (:meth:`definition`, byte-stable: a cached prefix), and
    what runs it. ``schema`` is strict: no property it doesn't name, the required ones listed."""

    name: str
    description: str
    schema: Mapping[str, Any]
    examples: tuple[Mapping[str, Any], ...]
    run: Run

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.schema),
            "strict": True,
            "input_examples": [dict(example) for example in self.examples],
        }


def block_tokens(block: Mapping[str, Any]) -> int:
    """The tokens a ``search_result`` block shows the model: its source, title and lines."""
    return count_tokens(block["source"] + block["title"]) + sum(
        count_tokens(part["text"]) for part in block["content"]
    )


def failed(error: ToolError) -> ToolResult:
    return ToolResult([{"type": "text", "text": error.text()}], [], error.message, is_error=True)


def clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1] + "…"


def block_spans(first_line: int, count: int) -> list[tuple[int, int]]:
    """``count`` lines from ``first_line`` in blocks of :data:`BLOCK_LINES`, the last one
    taken into the one before when it would be shorter than :data:`MIN_BLOCK_LINES`."""
    spans: list[tuple[int, int]] = []
    start = 0
    while start < count:
        end = min(start + BLOCK_LINES, count)
        if spans and end - start < MIN_BLOCK_LINES:
            first, _ = spans.pop()
            spans.append((first, first_line + end - 1))
        else:
            spans.append((first_line + start, first_line + end - 1))
        start = end
    return spans


def code_result(
    ctx: ToolContext,
    path: str,
    first_line: int,
    lines: Sequence[str],
    *,
    title: str | None = None,
    symbol_key: str | None = None,
) -> tuple[dict[str, Any], Source]:
    """``lines`` of ``path`` (the first numbered ``first_line``) as a ``search_result`` block,
    and what it cites."""
    return runs_result(ctx, path, [(first_line, lines)], title=title, symbol_key=symbol_key)


def runs_result(
    ctx: ToolContext,
    path: str,
    runs: Sequence[tuple[int, Sequence[str]]],
    *,
    title: str | None = None,
    symbol_key: str | None = None,
) -> tuple[dict[str, Any], Source]:
    """Runs of ``path``'s lines (each its first line's number, and its lines, in order) as one
    ``search_result`` block: each run in blocks of its own, and what they cite."""
    spans: list[tuple[int, int]] = []
    content: list[dict[str, str]] = []
    for first_line, lines in runs:
        for start, end in block_spans(first_line, len(lines)):
            spans.append((start, end))
            text = "\n".join(f"{n}\t{clip(lines[n - first_line])}" for n in range(start, end + 1))
            content.append({"type": "text", "text": text})
    block = {
        "type": "search_result",
        "source": ctx.source(path, spans[0][0], spans[-1][1]),
        "title": title or path,
        "content": content,
        "citations": {"enabled": True},
    }
    return block, Source(path, ctx.sha, tuple(spans), symbol_key)


def lines_within(lines: Sequence[str], budget: int) -> int:
    """How many of ``lines``, from the first, fit in ``budget`` tokens as a result shows them
    (each after its number)."""
    used = 0
    for taken, tokens in enumerate(count_each([f"0000\t{clip(line)}\n" for line in lines])):
        used += tokens
        if used > budget:
            return taken
    return len(lines)


class Results:
    """A tool's answer as it is put together, within :data:`MAX_RESULT_TOKENS`: what doesn't
    fit is counted, not sent, and the answer ends with a note of how much was left out."""

    def __init__(self, budget: int = MAX_RESULT_TOKENS) -> None:
        self.content: list[dict[str, Any]] = []
        self.sources: list[Source] = []
        self.left = budget - NOTE_TOKENS
        self.left_out = 0

    def text(self, text: str) -> bool:
        tokens = count_tokens(text)
        if tokens > self.left:
            self.left_out += 1
            return False
        self.left -= tokens
        self.content.append({"type": "text", "text": text})
        return True

    def lines(self, lines: Sequence[str], *, head: str | None = None) -> int:
        """As many of ``lines``, from the first, as fit, as one text (after ``head``, if any):
        how many."""
        used = count_tokens(head + "\n") if head else 0
        taken = 0
        for tokens in count_each([f"{line}\n" for line in lines]):
            if used + tokens > self.left:
                break
            used += tokens
            taken += 1
        if taken < len(lines):
            self.left_out += len(lines) - taken
        shown = [*([head] if head else []), *lines[:taken]]
        if shown and (taken or not lines):
            self.left -= used
            self.content.append({"type": "text", "text": "\n".join(shown)})
        return taken

    def note(self, text: str) -> None:
        """A short note (a page's, with its cursor) from the tokens kept back for notes: never
        left out, whatever the content took."""
        self.content.append({"type": "text", "text": text})

    def fits(self, block: dict[str, Any]) -> bool:
        return block_tokens(block) <= self.left

    def code(self, block: dict[str, Any], source: Source) -> bool:
        tokens = block_tokens(block)
        if tokens > self.left:
            self.left_out += 1
            return False
        self.left -= tokens
        self.content.append(block)
        self.sources.append(source)
        return True

    def done(self, summary: str, *, hits: int, narrow: str) -> ToolResult:
        """The answer; ``narrow`` says how to ask for less (a path, a layer, fewer lines)."""
        if self.left_out:
            what = "result was" if self.left_out == 1 else "results were"
            self.content.append(
                {
                    "type": "text",
                    "text": f"{self.left_out} more {what} left out to keep this answer short; "
                    f"{narrow}.",
                }
            )
        if not self.content:
            self.content.append({"type": "text", "text": "Nothing found."})
        return ToolResult(self.content, self.sources, summary, hits)


# ---------------------------------------------------------------- pages


def args_key(name: str, args: Mapping[str, Any]) -> str:
    """The call ``args`` make, but for its cursor: a cursor is only good for the same call."""
    given = {k: v for k, v in args.items() if k != "cursor"}
    return hashlib.sha256(f"{name}:{json.dumps(given, sort_keys=True)}".encode()).hexdigest()


def next_cursor(key: str, offset: int) -> str:
    raw = json.dumps({"k": key[:16], "o": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def offset_of(cursor: str | None, key: str) -> int:
    """Where the page ``cursor`` names starts (0: none given)."""
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        found = json.loads(raw)
        offset = found["o"]
        same = found["k"] == key[:16] and isinstance(offset, int) and not isinstance(offset, bool)
    except (ValueError, KeyError, TypeError):
        same, offset = False, 0
    if not same or not isinstance(offset, int) or offset < 0:
        raise ToolError(
            "That cursor belongs to another call. Repeat the call with the same arguments and "
            "the next_cursor its own result gave, or without a cursor for the first page."
        )
    return offset


def page_note(offset: int, shown: int, total: int, key: str, *, exact: bool, narrow: str) -> str:
    """``Showing 1-8 of 143`` (``of 143+`` when there may be more), and how to read on."""
    if shown == 0:
        return f"No more results past {offset}." if offset else "Nothing found."
    of = f"{total}" if exact else f"{total}+"
    note = f"Showing {offset + 1}-{offset + shown} of {of}."
    if offset + shown < total or not exact:
        note += f" next_cursor: {next_cursor(key, offset + shown)} (or {narrow})."
    return note


# ---------------------------------------------------------------- ids


def readable_id(symbol_key: str, kind: str, path: str | None, qualified_name: str) -> str:
    """A definition as ``sym:<path>::<Qualified.name>``; a node of the graph that is no
    definition (an endpoint, a request, a table) by its key (``api_endpoint POST /items/``)."""
    if path is None or kind not in SYMBOL_KINDS or not qualified_name:
        return symbol_key
    return f"sym:{path}::{qualified_name}"


FIND_EXAMPLE = 'find_symbol({"name": "create_item"})'


def require_graph(ctx: ToolContext) -> GraphSnapshot:
    """The snapshot's graph, or why a graph tool can't answer yet."""
    if ctx.graph is None:
        raise ToolError(
            "The graph of this commit isn't built yet. Use find_symbol, code_search and "
            "read_code meanwhile."
        )
    return ctx.graph


async def symbol_key_of(ctx: ToolContext, ref: str) -> str:
    """The node key a tool's ``symbol`` argument means: a readable id (the first of that name
    in its file, by line), or a key as it is."""
    ref = ref.strip()
    if not ref.startswith("sym:"):
        return ref
    path, sep, qualified = ref.removeprefix("sym:").partition("::")
    if not sep or not path or not qualified:
        raise ToolError(
            f"{ref!r} is not a symbol id: one is sym:<path>::<qualified name>.",
            example='graph_neighbors({"symbol": "sym:backend/app/crud.py::create_item"})',
        )
    found = await ctx.db.scalar(
        select(CodeNode.symbol_key)
        .join(RepoFile, RepoFile.id == CodeNode.file_id)
        .where(
            CodeNode.snapshot_id == ctx.snapshot_id,
            RepoFile.path == path,
            CodeNode.qualified_name == qualified,
        )
        .order_by(CodeNode.start_line)
        .limit(1)
    )
    if found is None:
        raise ToolError(
            f"Nothing in {path} at this commit is named {qualified}. Find its id with "
            "find_symbol first.",
            example=FIND_EXAMPLE,
        )
    return found


# ---------------------------------------------------------------- files


def file_path(ctx: ToolContext, path: str, *, example: str) -> str:
    """``path`` as the snapshot records it (a leading ``./`` or ``/`` dropped), or why not."""
    try:
        cleaned = check_path(path.strip().removeprefix("./").lstrip("/"))
    except UnsafePathError as exc:
        raise ToolError(f"{path!r} is not a path in the repository.", example=example) from exc
    return cleaned


def file_lines(ctx: ToolContext, path: str, *, example: str) -> list[str]:
    """The lines of a file of the snapshot (text, decoded as the code browser decodes it)."""
    path = file_path(ctx, path, example=example)
    if not _recorded(ctx.tree, path):
        raise ToolError(
            f"There is no file {path} at this commit. Find files with list_dir, or search "
            "for the code with code_search.",
            example=example,
        )
    try:
        data = read_file(ctx.root, path, max_bytes=MAX_FILE_BYTES)
    except ValueError as exc:
        raise ToolError(f"{path} is too large to read here; grep it instead.") from exc
    except OSError as exc:
        raise ToolError(f"{path} can't be read at this commit.") from exc
    decoded = decode_text(data)
    if decoded is None:
        raise ToolError(f"{path} is a binary file, not text.")
    return text_lines(decoded[0])


def _recorded(tree: Sequence[str], path: str) -> bool:
    at = bisect_left(tree, path)
    return at < len(tree) and tree[at] == path
