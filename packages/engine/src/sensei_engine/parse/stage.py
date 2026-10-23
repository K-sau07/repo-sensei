"""The parse stage: every file of a working copy we have a grammar for, parsed in parallel.

Each file comes back as a :class:`FileParse`: its status, its syntax error count and how many
facts each query captured. Nothing a file holds stops the stage: syntax errors are counted, slow
files time out, unreadable files and files that crash the parser are marked and passed over.
:class:`ParseReport` adds them up for the job's stage details.

Files are read inside the worker processes, so only paths cross between processes. A path must
stay inside the working copy: an absolute path, a ``..`` or a symbolic link out of it makes the
file unreadable, and the file itself is opened without following links.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sensei_engine.ingest.classify import MAX_PARSE_BYTES
from sensei_engine.parse.languages import SUPPORTED_LANGUAGES
from sensei_engine.parse.parser import TIMEOUT_SECONDS, ParseStatus, parse_file
from sensei_engine.parse.pool import BATCH_SIZE, Failed, run_parallel

EXAMPLES = 10  # paths kept per problem, for the stage details


@dataclass(frozen=True, slots=True)
class ParseRequest:
    root: str
    path: str  # relative, POSIX
    language: str


@dataclass(frozen=True, slots=True)
class FileParse:
    path: str
    language: str
    status: ParseStatus
    error_count: int = 0
    seconds: float = 0.0
    captures: dict[str, int] = field(default_factory=dict[str, int])  # matches per query kind
    failure: str | None = None  # why a failed file failed


def read_source(root: Path, path: str) -> bytes:
    """The file's bytes (up to one past the parse limit). Raises ``OSError`` when it can't be
    read, or isn't inside ``root``."""
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise PermissionError(path)
    base = root.resolve()
    full = base / relative
    if not full.parent.resolve().is_relative_to(base):  # a linked directory on the way
        raise PermissionError(path)
    fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_PARSE_BYTES + 1)


def parse_request(request: ParseRequest) -> FileParse:
    """Parse one file of the working copy (the pool's work: runs in a worker process)."""
    try:
        data = read_source(Path(request.root), request.path)
    except OSError as exc:
        return FileParse(
            request.path, request.language, ParseStatus.UNREADABLE, failure=type(exc).__name__
        )
    result = parse_file(request.path, data, language=request.language)
    return FileParse(
        path=request.path,
        language=request.language,
        status=result.status,
        error_count=result.error_count,
        seconds=result.seconds,
        captures={kind.value: len(matches) for kind, matches in result.captures.items()},
    )


def parseable(language: str | None) -> bool:
    return language in SUPPORTED_LANGUAGES


def parse_files(
    root: Path,
    files: Sequence[tuple[str, str]],
    *,
    workers: int | None = None,
    batch_size: int = BATCH_SIZE,
) -> Iterator[FileParse]:
    """Parse ``files`` (``(path, language)`` pairs, languages we parse) under ``root``, as
    batches finish."""
    requests = [ParseRequest(str(root), path, language) for path, language in files]
    for request, result in run_parallel(
        parse_request,
        requests,
        workers=workers,
        batch_size=batch_size,
        item_timeout=TIMEOUT_SECONDS,
    ):
        if isinstance(result, Failed):
            status = ParseStatus.TIMED_OUT if result.reason == "timeout" else ParseStatus.FAILED
            yield FileParse(
                request.path,
                request.language,
                status,
                failure=f"{result.reason}: {result.message}",
            )
        else:
            yield result


@dataclass(slots=True)
class ParseReport:
    """The stage's totals, and a few example paths for each kind of problem."""

    files: int = 0
    parsed: int = 0
    with_errors: int = 0
    error_nodes: int = 0
    seconds: float = 0.0  # parse time summed over files (not wall time)
    statuses: dict[str, int] = field(default_factory=dict[str, int])
    captures: dict[str, int] = field(default_factory=dict[str, int])
    examples: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def add(self, file: FileParse) -> None:
        self.files += 1
        self.seconds += file.seconds
        self.statuses[file.status.value] = self.statuses.get(file.status.value, 0) + 1
        for kind, count in file.captures.items():
            self.captures[kind] = self.captures.get(kind, 0) + count
        if file.status is ParseStatus.PARSED:
            self.parsed += 1
            if file.error_count:
                self.with_errors += 1
                self.error_nodes += file.error_count
                self._example("syntax_errors", file.path)
        else:
            self._example(file.status.value, file.path)

    def _example(self, problem: str, path: str) -> None:
        paths = self.examples.setdefault(problem, [])
        if len(paths) < EXAMPLES:
            paths.append(path)

    def details(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "parsed": self.parsed,
            "with_syntax_errors": self.with_errors,
            "syntax_error_nodes": self.error_nodes,
            "statuses": dict(sorted(self.statuses.items())),
            "captures": dict(sorted(self.captures.items())),
            "parse_seconds": round(self.seconds, 3),
            "examples": {k: sorted(v) for k, v in sorted(self.examples.items())},
        }
