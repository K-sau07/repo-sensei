"""A repository's history, read for an investigation (8.5): who changed a file and when, which
commit wrote a line, what a commit changed.

Read from a checkout with history (made by the worker); nothing in it runs: git is called with
a fixed argv (never a shell), without the user's or system's configuration, hooks, external
diff or text-conversion drivers, so a repository can't make it run anything. Commits and paths
are checked before they reach git (``--`` before paths); output is capped as it is read.

The history is shallow (the worker fetches the latest commits): its oldest commit stands for
everything before it (blame credits it with every older line, its diff adds the whole tree), so
blame says where the history stops, and that commit is never a suspect.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sensei_engine.snapshots import UnsafePathError, check_path

TIMEOUT_SECONDS = 20.0
MAX_OUTPUT_BYTES = 256 * 1024
MAX_LOG = 50
MAX_BLAME_LINES = 200
MAX_SHOW_LINES = 400
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")  # SHA-1, or a SHA-256 repository's
_SEP = "\x1f"  # between a log line's fields (no subject holds it)
_SAFE_CONFIG = (
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "diff.external=",
    "-c", "core.pager=cat", "-c", "color.ui=false", "-c", "log.showSignature=false",
)  # fmt: skip


class HistoryError(Exception):
    """A history question that can't be answered (what, said for the model)."""


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    author: str
    date: str  # ISO 8601
    subject: str


@dataclass(frozen=True, slots=True)
class BlameLine:
    line: int
    sha: str
    author: str
    date: str
    summary: str
    text: str
    boundary: bool = False  # where the fetched history stops: an older commit wrote it


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               HOME=os.devnull, GIT_OPTIONAL_LOCKS="0")  # fmt: skip
    return env


def commit_id(value: object) -> str:
    if not isinstance(value, str) or not _SHA.match(value):
        raise HistoryError(f"{value!r} isn't a commit id (7 to 64 hex digits).")
    return value.lower()


def repo_path(value: object) -> str:
    if not isinstance(value, str):
        raise HistoryError("A path is a string: a file of the repository.")
    try:
        path = check_path(value)
    except UnsafePathError:
        raise HistoryError(f"{value!r} isn't a path in the repository.") from None
    if not path:
        raise HistoryError("Name a file.")
    return path


class GitHistory:
    """The history of the checkout at ``root``, up to ``head`` (the commit being fixed)."""

    def __init__(self, root: Path, head: str) -> None:
        self.root = root
        self.head = commit_id(head)

    async def _git(self, *args: str) -> str:
        return await git(self.root, *args)

    async def resolve(self, sha: str) -> str | None:
        """The full id of the commit ``sha`` names in the history before ``head`` (or ``head``),
        unless it is where a shallow history stops (it stands for every older commit)."""
        try:
            found = (
                await self._git("rev-parse", "--verify", "--quiet", f"{commit_id(sha)}^{{commit}}")
            ).strip()
            await self._git("merge-base", "--is-ancestor", found, self.head)
        except HistoryError:
            return None
        return None if found in await self.shallow() else found

    async def shallow(self) -> frozenset[str]:
        """The commits a shallow history stops at (none: the history is whole)."""
        try:
            where = (await self._git("rev-parse", "--git-path", "shallow")).strip()
        except HistoryError:
            return frozenset()
        path = Path(where) if Path(where).is_absolute() else self.root / where
        try:
            return frozenset(path.read_text().split())
        except OSError:
            return frozenset()

    async def exists(self, sha: str) -> bool:
        """``sha`` names a commit in the history before ``head`` (or ``head``), not where a
        shallow history stops."""
        return await self.resolve(sha) is not None

    async def log(self, path: object = None, limit: int = 20) -> list[Commit]:
        """The commits before ``head`` that changed ``path`` (any, without one), newest first."""
        args = ["log", f"--max-count={max(1, min(limit, MAX_LOG))}", "--no-merges",
                f"--format=%H{_SEP}%an{_SEP}%aI{_SEP}%s", self.head]  # fmt: skip
        if path is not None:
            args += ["--", repo_path(path)]
        commits: list[Commit] = []
        for line in (await self._git(*args)).splitlines():
            parts = line.split(_SEP)
            if len(parts) == 4:
                commits.append(Commit(parts[0], parts[1], parts[2], parts[3]))
        return commits

    async def blame(self, path: object, start: int, end: int) -> list[BlameLine]:
        """Which commit last changed each of the lines ``start``-``end`` of ``path`` at
        ``head``."""
        if start < 1 or end < start:
            raise HistoryError("Give lines as start_line <= end_line, from 1.")
        end = min(end, start + MAX_BLAME_LINES - 1)
        out = await self._git(
            "blame", "--porcelain", f"-L{start},{end}", self.head, "--", repo_path(path)
        )
        return _porcelain(out)

    async def show(self, sha: str, path: object = None) -> str:
        """What a commit changed: its message and files, and the diff (of ``path`` alone, when
        given), at most :data:`MAX_SHOW_LINES` lines."""
        args = ["show", "--no-ext-diff", "--no-textconv", "--stat", "--patch",
                "--format=commit %H%nAuthor: %an%nDate: %aI%n%n%B", commit_id(sha)]  # fmt: skip
        if path is not None:
            args += ["--", repo_path(path)]
        lines = (await self._git(*args)).splitlines()
        if len(lines) > MAX_SHOW_LINES:
            left = len(lines) - MAX_SHOW_LINES
            lines = [*lines[:MAX_SHOW_LINES], f"[{left} more lines: name a path to see less]"]
        return "\n".join(lines)


async def git(
    root: Path,
    *args: str,
    cap: int | None = None,
    stdin: bytes | None = None,
    whole: bool = False,
) -> str:
    """git's output for ``args`` in the checkout at ``root`` (safely configured, see the
    module): at most ``cap`` bytes of it (:data:`MAX_OUTPUT_BYTES` by default), the rest
    unread, unless ``whole`` (then more is an error). :class:`HistoryError` with what git said
    when it fails."""
    cap = MAX_OUTPUT_BYTES if cap is None else cap
    process = await asyncio.create_subprocess_exec(
        "git", *_SAFE_CONFIG, *args,
        cwd=root, env=_env(),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    assert process.stdout is not None  # noqa: S101 - piped above
    assert process.stderr is not None  # noqa: S101

    async def feed() -> None:
        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin)
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.drain()
            process.stdin.close()

    try:
        _, (out, cut), (err, _) = await asyncio.wait_for(
            asyncio.gather(feed(), _upto(process.stdout, cap), _upto(process.stderr, 16 * 1024)),
            TIMEOUT_SECONDS,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise HistoryError("git took too long; ask about less.") from None
    if cut:  # enough of it: what's left isn't read (a commit adding a huge file)
        with contextlib.suppress(ProcessLookupError):  # it may have finished meanwhile
            process.kill()
    await process.wait()
    if cut and whole:
        raise HistoryError(f"git's answer is over {cap} bytes.")
    if process.returncode != 0 and not cut:
        said = err.decode("utf-8", "replace").strip().splitlines()
        raise HistoryError(said[-1] if said else "git couldn't answer that.")
    return out.decode("utf-8", "replace")


async def _upto(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    """At most ``cap`` bytes of ``stream`` (and whether there was more)."""
    data = bytearray()
    while len(data) <= cap:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return bytes(data), False
        data += chunk
    return bytes(data[:cap]), True


def _iso(epoch: str) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), UTC).isoformat()
    except ValueError:
        return ""


def _porcelain(out: str) -> list[BlameLine]:
    """``git blame --porcelain``'s lines: each a header (sha, original and final line), the
    commit's facts the first time it appears, then the line's text after a tab."""
    facts: dict[str, dict[str, str]] = {}
    found: list[BlameLine] = []
    sha, final = "", 0
    for line in out.splitlines():
        if line.startswith("\t"):
            known = facts.get(sha, {})
            found.append(
                BlameLine(
                    line=final, sha=sha, author=known.get("author", ""),
                    date=_iso(known.get("author-time", "")), summary=known.get("summary", ""),
                    text=line[1:], boundary="boundary" in known,
                )
            )  # fmt: skip
            continue
        head = line.split(" ")
        if len(head) >= 3 and _SHA.match(head[0]) and len(head[0]) in (40, 64):
            sha, final = head[0], int(head[2])
            facts.setdefault(sha, {})
        elif sha and line == "boundary":
            facts[sha]["boundary"] = "1"
        elif sha and " " in line:
            key, value = line.split(" ", 1)
            facts[sha].setdefault(key, value)
    return found
