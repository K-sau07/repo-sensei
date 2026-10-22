"""Exact search: literal or regex matches in an unpacked snapshot, with ripgrep.

``rg --json`` runs in the snapshot's directory; each match comes back as a :class:`GrepHit`
(path, line, column, the line's text and the lines around it). Hits are in path order
(``--sort path``, which also keeps a search on one thread), so the same search gives the same
hits.

Safety:

- the pattern and globs reach ``rg`` as single arguments (``--regexp=…``, ``--glob=…``), never
  through a shell, with ``--no-config`` and an empty environment, so nothing outside the call
  changes what it does;
- the search runs in the snapshot's directory (a real directory, not a link) and never follows
  symbolic links, so it reads nothing outside it. Ignore files are not honoured (a repository
  can't hide its own files); with ``files``, only matches in those paths count (what discovery
  recorded), and the snapshot cache's marker is never searched;
- ripgrep's default regex engine runs in time linear in the input (no backtracking; PCRE2 is
  never turned on), the compiled pattern is capped (``--regex-size-limit`` and
  ``--dfa-size-limit``), a pattern is at most 1,000 characters on one line, and a search stops
  after 3 s and once it has ``limit`` hits. One :class:`ExactSearch` runs at most four searches
  at a time;
- rg's output is read one record at a time, each at most 1 MiB: a line longer than that (minified
  code) is counted in ``long_lines`` instead of being read.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import enum
import os
import shutil
import signal
from collections import deque
from collections.abc import Container
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import orjson

from sensei_engine.snapshots import READY_MARKER

RG_BINARY = "rg"
TIMEOUT_SECONDS = 3.0
MAX_CONCURRENT = 4
MAX_PATTERN_CHARS = 1000
MAX_GLOBS = 20
MAX_GLOB_CHARS = 256
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
MAX_CONTEXT = 10
MAX_LINE_CHARS = 500  # of each line in a hit; a longer line is cut around its first match
MAX_RANGES = 50  # matches located on one line
MAX_RECORD_BYTES = 1024 * 1024  # one line of rg's output
REGEX_SIZE_LIMIT = "10M"  # the compiled pattern, and the lazy DFA's cache
STDERR_BYTES = 8192

# How rg starts a complaint about the pattern or a glob (anything else is a failure on our side).
_QUERY_ERRORS = (
    "regex parse error",
    "error parsing glob",
    "compiled regex exceeds size limit",
    "the literal",
)


class Case(enum.StrEnum):
    SENSITIVE = "sensitive"
    INSENSITIVE = "insensitive"
    SMART = "smart"  # insensitive unless the pattern has an upper-case letter


_CASE_FLAGS = {
    Case.SENSITIVE: "--case-sensitive",
    Case.INSENSITIVE: "--ignore-case",
    Case.SMART: "--smart-case",
}


class InvalidQueryError(ValueError):
    """A pattern or glob that can't be searched for; the message says why."""


class GrepUnavailableError(RuntimeError):
    """ripgrep isn't installed, or failed."""


class GrepBusyError(RuntimeError):
    """Every search slot stayed taken for the whole time limit."""


@dataclass(frozen=True, slots=True)
class GrepQuery:
    pattern: str
    regex: bool = False  # else a literal string
    case: Case = Case.SENSITIVE
    globs: tuple[str, ...] = ()  # rg globs over paths; "!" in front excludes
    limit: int = DEFAULT_LIMIT  # hits
    context: int = 0  # lines before and after each hit

    def __post_init__(self) -> None:
        if not self.pattern:
            raise InvalidQueryError("Give a pattern to search for.")
        if len(self.pattern) > MAX_PATTERN_CHARS:
            raise InvalidQueryError(f"Patterns are at most {MAX_PATTERN_CHARS} characters.")
        if "\n" in self.pattern or "\0" in self.pattern:
            raise InvalidQueryError("A pattern matches within one line.")
        if len(self.globs) > MAX_GLOBS:
            raise InvalidQueryError(f"At most {MAX_GLOBS} path globs.")
        for glob in self.globs:
            if not glob or len(glob) > MAX_GLOB_CHARS or "\n" in glob or "\0" in glob:
                raise InvalidQueryError(f"Path globs are 1 to {MAX_GLOB_CHARS} characters.")
        if not 1 <= self.limit <= MAX_LIMIT:
            raise InvalidQueryError(f"The limit is 1 to {MAX_LIMIT} hits.")
        if not 0 <= self.context <= MAX_CONTEXT:
            raise InvalidQueryError(f"Context is 0 to {MAX_CONTEXT} lines.")


@dataclass(slots=True)
class GrepHit:
    path: str
    line: int  # 1-based
    col: int  # 1-based, in characters: where the first match on the line starts
    text: str  # the line, or MAX_LINE_CHARS of it starting after ``offset`` characters
    offset: int = 0
    # Each match on the line (up to MAX_RANGES): 1-based start column, end column exclusive.
    ranges: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    before: list[str] = field(default_factory=list[str])  # the lines just above, in order
    after: list[str] = field(default_factory=list[str])  # the lines just below


@dataclass(slots=True)
class GrepResult:
    hits: list[GrepHit]
    truncated: bool  # there are more matches than ``limit``
    timed_out: bool  # the time limit stopped the search: there may be more matches
    long_lines: int  # lines too long to read; a match on one isn't among the hits


def _argv(binary: str, query: GrepQuery) -> list[str]:
    argv = [
        binary,
        "--json",
        "--no-config",
        "--no-ignore",
        "--hidden",
        "--no-messages",  # unreadable files are skipped quietly; pattern errors still show
        "--sort=path",
        f"--max-count={query.limit + 1}",  # per file; one more says whether there are more
        f"--regex-size-limit={REGEX_SIZE_LIMIT}",
        f"--dfa-size-limit={REGEX_SIZE_LIMIT}",
        _CASE_FLAGS[query.case],
    ]
    if not query.regex:
        argv.append("--fixed-strings")
    if query.context:
        argv.append(f"--context={query.context}")
    argv += [f"--glob={glob}" for glob in query.globs]
    argv.append(f"--glob=!/{READY_MARKER}")  # last, so no glob before it lets the marker in
    argv += [f"--regexp={query.pattern}", "--", "."]
    return argv


def _bytes(value: dict[str, Any]) -> bytes:
    """rg's ``{"text": …}`` (UTF-8) or ``{"bytes": base64}`` (anything else)."""
    text = value.get("text")
    if text is not None:
        return str(text).encode()
    return base64.b64decode(value.get("bytes", ""))


def _line(value: dict[str, Any]) -> bytes:
    return _bytes(value).removesuffix(b"\n").removesuffix(b"\r")


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _path(value: dict[str, Any]) -> str | None:
    text = value.get("text")
    if not isinstance(text, str):
        return None  # not UTF-8: discovery records no such path
    return text.removeprefix("./")


def _columns(raw: bytes, offsets: list[int]) -> dict[int, int]:
    """Byte offsets into ``raw`` as 0-based character offsets, decoding each stretch once."""
    found: dict[int, int] = {}
    at_byte = at_char = 0
    for offset in sorted(set(offsets)):
        at_char += len(_decode(raw[at_byte:offset]))
        at_byte = offset
        found[offset] = at_char
    return found


def _hit(path: str, data: dict[str, Any]) -> GrepHit:
    raw = _line(data["lines"])
    line = _decode(raw)
    spans = [
        (min(max(int(sub["start"]), 0), len(raw)), min(max(int(sub["end"]), 0), len(raw)))
        for sub in data.get("submatches", [])[:MAX_RANGES]
    ]
    chars = _columns(raw, [offset for span in spans for offset in span])
    ranges = [(chars[start] + 1, chars[end] + 1) for start, end in spans]
    col = ranges[0][0] if ranges else 1
    offset = 0
    if len(line) > MAX_LINE_CHARS and ranges and ranges[0][1] - 1 > MAX_LINE_CHARS:
        offset = max(0, min(col - 1 - MAX_LINE_CHARS // 5, len(line) - MAX_LINE_CHARS))
    return GrepHit(
        path=path,
        line=int(data["line_number"]),
        col=col,
        text=line[offset : offset + MAX_LINE_CHARS],
        offset=offset,
        ranges=ranges,
    )


class _Collector:
    """Hits from rg's records, as they arrive."""

    def __init__(self, query: GrepQuery, files: Container[str] | None) -> None:
        self._query = query
        self._files = files
        self.hits: list[GrepHit] = []
        self.truncated = False
        self.long_lines = 0
        self.summary = False  # rg got to the end of the search
        self._path: str | None = None  # the file being read, when its matches count
        self._recent: deque[tuple[int, str]] = deque(maxlen=query.context)
        self._waiting: list[GrepHit] = []  # hits still collecting lines after them

    async def read(self, stream: asyncio.StreamReader) -> bool:
        """Read until rg's output ends (True) or nothing more is needed (False)."""
        overlong = False
        while True:
            try:
                record = await stream.readline()
            except ValueError:  # over MAX_RECORD_BYTES; the rest of it comes next
                if not overlong:
                    self.long_lines += 1
                overlong = True
                continue
            if not record:
                return True
            try:
                message = orjson.loads(record)
            except orjson.JSONDecodeError:
                overlong = False  # the end of an overlong record
                continue
            overlong = False
            if isinstance(message, dict) and not self._take(cast("dict[str, Any]", message)):
                return False

    def _take(self, message: dict[str, Any]) -> bool:
        """One record; False once nothing more is needed."""
        kind = message.get("type")
        data: dict[str, Any] = message.get("data") or {}
        if kind == "begin":
            path = _path(data.get("path") or {})
            counts = path is not None and (self._files is None or path in self._files)
            self._path = path if counts else None
            self._recent.clear()
            self._waiting.clear()
        elif kind == "end":
            self._path = None
            self._waiting.clear()
            return not self.truncated
        elif kind == "summary":
            self.summary = True
        elif kind in ("match", "context") and self._path is not None:
            self._line(kind, data, self._path)
            return not (self.truncated and not self._waiting)
        return True

    def _line(self, kind: str, data: dict[str, Any], path: str) -> None:
        context = self._query.context
        number = int(data["line_number"])
        text = _decode(_line(data["lines"]))[:MAX_LINE_CHARS]
        for hit in self._waiting:
            if number == hit.line + len(hit.after) + 1:  # only the unbroken run below it
                hit.after.append(text)
        self._waiting = [
            hit for hit in self._waiting if hit.line + len(hit.after) == number < hit.line + context
        ]
        if kind == "match":
            if len(self.hits) < self._query.limit:
                hit = _hit(path, data)
                expected = number - 1
                for seen, line in reversed(self._recent):  # the unbroken run just above it
                    if seen != expected or len(hit.before) == context:
                        break
                    hit.before.insert(0, line)
                    expected -= 1
                self.hits.append(hit)
                if context:
                    self._waiting.append(hit)
            else:
                self.truncated = True
        self._recent.append((number, text))


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    """The first ``cap`` bytes; the rest is read and dropped (so the writer never blocks)."""
    kept = bytearray()
    while chunk := await stream.read(65536):
        if len(kept) < cap:
            kept += chunk[: cap - len(kept)]
    return bytes(kept)


def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # already gone (macOS: EPERM)
        os.killpg(process.pid, signal.SIGKILL)


class ExactSearch:
    """Runs searches, at most ``max_concurrent`` at a time (others wait their turn, for up to
    the time limit)."""

    def __init__(
        self,
        *,
        binary: str = RG_BINARY,
        timeout: float = TIMEOUT_SECONDS,
        max_concurrent: int = MAX_CONCURRENT,
    ) -> None:
        self._binary = binary
        self._timeout = timeout
        self._slots = asyncio.Semaphore(max_concurrent)

    async def search(
        self, root: Path, query: GrepQuery, *, files: Container[str] | None = None
    ) -> GrepResult:
        """Matches for ``query`` under ``root``; with ``files``, only in those paths (relative,
        ``/``-separated). :class:`InvalidQueryError` when rg can't use the pattern or a glob."""
        binary = await asyncio.to_thread(self._locate, root)
        try:
            async with asyncio.timeout(self._timeout):
                await self._slots.acquire()
        except TimeoutError:
            raise GrepBusyError("every search slot is taken") from None
        try:
            return await self._run(binary, root, query, files)
        finally:
            self._slots.release()

    def _locate(self, root: Path) -> str:
        """rg's path, once ``root`` is known to be a real directory."""
        if root.is_symlink() or not root.is_dir():
            raise FileNotFoundError(root)
        binary = shutil.which(self._binary)
        if binary is None:
            raise GrepUnavailableError(f"{self._binary} is not installed")
        return binary

    async def _run(
        self, binary: str, root: Path, query: GrepQuery, files: Container[str] | None
    ) -> GrepResult:
        process = await asyncio.create_subprocess_exec(
            *_argv(binary, query),
            cwd=root,
            env={},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_RECORD_BYTES,
            start_new_session=True,  # so a timeout can stop it and anything it started
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("rg's output isn't piped")
        errors = asyncio.create_task(_read_capped(process.stderr, STDERR_BYTES))
        collector = _Collector(query, files)
        ended = timed_out = False
        try:
            async with asyncio.timeout(self._timeout):
                ended = await collector.read(process.stdout)
                if ended:
                    await process.wait()
        except TimeoutError:
            # Past the limit only context was still to come; at the end, only the exit.
            timed_out = not (ended or collector.truncated)
        finally:
            if process.returncode is None:
                _kill(process)
                await process.wait()
            stderr = await errors
        # A search that ran to the end closes with a summary, even when some files couldn't be
        # read (exit 2); without one, rg refused the query or failed.
        if ended and not collector.summary:
            message = _decode(stderr).strip().removeprefix("rg: ")
            if message.startswith(_QUERY_ERRORS):
                raise InvalidQueryError(message[:1000])
            raise GrepUnavailableError(f"rg exited with {process.returncode}: {message[:1000]}")
        return GrepResult(
            hits=collector.hits,
            truncated=collector.truncated,
            timed_out=timed_out,
            long_lines=collector.long_lines,
        )
