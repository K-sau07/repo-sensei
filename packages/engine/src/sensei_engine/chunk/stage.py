"""The chunks stage: every file of a working copy worth searching, chunked in parallel.

Which files: everything read as text (not binary, too large or an LFS pointer), except vendored
code, and generated files that aren't code we parse (lockfiles, minified CSS: noise to keyword
and semantic search alike). Generated code we parse (``*.gen.ts`` clients, ``*_pb2.py``) stays.

Code is parsed again here, in the worker processes, rather than carried over from the parse
stage: its trees can't leave those processes, and the chunks of a large repository wouldn't fit
in memory. A file that fails to chunk is marked and passed over, as in the parse stage; a parse
that hangs is stopped by killing its worker.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sensei_engine.chunk.files import chunk_file
from sensei_engine.chunk.model import Chunk
from sensei_engine.ingest.classify import Flag
from sensei_engine.parse.parser import TIMEOUT_SECONDS
from sensei_engine.parse.pool import BATCH_SIZE, Failed, run_parallel
from sensei_engine.parse.stage import EXAMPLES, parseable, read_source

# Never read as text: nothing to chunk.
UNREADABLE = frozenset({Flag.BINARY, Flag.TOO_LARGE, Flag.LFS_POINTER})
# Lockfiles the classifier doesn't flag as generated (package-lock.json is, by name).
LOCKFILES = frozenset(
    {
        "bun.lock",
        "Cargo.lock",
        "composer.lock",
        "Gemfile.lock",
        "Pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)
CHUNK_TIMEOUT_SECONDS = TIMEOUT_SECONDS * 5  # parsing, extracting and chunking a large file


def chunkable(path: str, language: str | None, flags: Sequence[str]) -> bool:
    """Whether a file of the snapshot (with its classifier flags) is chunked."""
    found = set(flags)
    if found & UNREADABLE or Flag.VENDORED in found:
        return False
    if PurePosixPath(path).name in LOCKFILES:
        return False
    return Flag.GENERATED not in found or parseable(language)


@dataclass(frozen=True, slots=True)
class ChunkRequest:
    root: str
    path: str  # relative, POSIX
    language: str | None


@dataclass(frozen=True, slots=True)
class FileChunked:
    path: str
    language: str | None
    chunks: list[Chunk] = field(default_factory=list[Chunk])
    failure: str | None = None  # unreadable, failed or timed out


def chunk_request(request: ChunkRequest) -> FileChunked:
    """Chunk one file of the working copy (the pool's work: runs in a worker process)."""
    try:
        data = read_source(Path(request.root), request.path)
    except OSError as exc:
        return FileChunked(request.path, request.language, failure=f"unreadable: {exc}"[:500])
    chunks = chunk_file(request.path, request.language, data)
    return FileChunked(request.path, request.language, chunks)


def chunk_files(
    root: Path,
    files: Sequence[tuple[str, str | None]],
    *,
    workers: int | None = None,
    batch_size: int = BATCH_SIZE,
) -> Iterator[FileChunked]:
    """Chunk ``files`` (``(path, language)`` pairs) under ``root``, as batches finish."""
    requests = [ChunkRequest(str(root), path, language) for path, language in files]
    for request, result in run_parallel(
        chunk_request,
        requests,
        workers=workers,
        batch_size=batch_size,
        item_timeout=CHUNK_TIMEOUT_SECONDS,
    ):
        if isinstance(result, Failed):
            failure = f"{result.reason}: {result.message}"
            yield FileChunked(request.path, request.language, failure=failure)
        else:
            yield result


@dataclass(slots=True)
class ChunkReport:
    """The stage's totals, and a few example paths of files that failed."""

    files: int = 0
    chunked: int = 0
    failed: int = 0
    chunks: int = 0
    tokens: int = 0
    max_tokens: int = 0
    split: int = 0  # chunks that are a part of a split definition
    kinds: dict[str, int] = field(default_factory=dict[str, int])
    examples: list[str] = field(default_factory=list[str])

    def add(self, file: FileChunked) -> None:
        self.files += 1
        if file.failure is not None:
            self.failed += 1
            if len(self.examples) < EXAMPLES:
                self.examples.append(file.path)
            return
        self.chunked += 1
        for chunk in file.chunks:
            self.chunks += 1
            self.tokens += chunk.tokens
            self.max_tokens = max(self.max_tokens, chunk.tokens)
            self.split += chunk.part is not None
            self.kinds[chunk.kind.value] = self.kinds.get(chunk.kind.value, 0) + 1

    def details(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "chunked": self.chunked,
            "failed": self.failed,
            "chunks": self.chunks,
            "kinds": dict(sorted(self.kinds.items())),
            "tokens": self.tokens,
            "mean_tokens": round(self.tokens / self.chunks) if self.chunks else 0,
            "max_tokens": self.max_tokens,
            "split_parts": self.split,
            "examples": {"failed": sorted(self.examples)} if self.examples else {},
        }
