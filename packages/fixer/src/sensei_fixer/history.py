"""A repository's history, read for an investigation (8.5): who changed a file and when, which
commit wrote a line, what a commit changed.

Read from a checkout with history (made by the worker); nothing in it runs: git is called with
a fixed argv (never a shell), without the user's or system's configuration, hooks, external
diff or text-conversion drivers, so a repository can't make it run anything. Commits and paths
are checked before they reach git (``--`` before paths); output is capped.
"""

from __future__ import annotations

import asyncio
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
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
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


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               HOME=os.devnull, GIT_OPTIONAL_LOCKS="0")  # fmt: skip
    return env


def commit_id(value: str) -> str:
    if not _SHA.match(value):
        raise HistoryError(f"{value!r} isn't a commit id (7 to 40 hex digits).")
    return value.lower()


def repo_path(value: str) -> str:
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
        process = await asyncio.create_subprocess_exec(
            "git", *_SAFE_CONFIG, *args,
            cwd=self.root, env=_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            out, err = await asyncio.wait_for(process.communicate(), TIMEOUT_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise HistoryError("git took too long; ask about less.") from None
        if process.returncode != 0:
            said = err.decode("utf-8", "replace").strip().splitlines()
            raise HistoryError(said[-1] if said else "git couldn't answer that.")
        return out[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")

    async def exists(self, sha: str) -> bool:
        """``sha`` names a commit in the history before ``head`` (or ``head``)."""
        try:
            found = await self._git(
                "rev-parse", "--verify", "--quiet", f"{commit_id(sha)}^{{commit}}"
            )
            await self._git("merge-base", "--is-ancestor", found.strip(), self.head)
        except HistoryError:
            return False
        return True

    async def log(self, path: str | None = None, limit: int = 20) -> list[Commit]:
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

    async def blame(self, path: str, start: int, end: int) -> list[BlameLine]:
        """Which commit last changed each of the lines ``start``-``end`` of ``path`` at
        ``head``."""
        if start < 1 or end < start:
            raise HistoryError("Give lines as start_line <= end_line, from 1.")
        end = min(end, start + MAX_BLAME_LINES - 1)
        out = await self._git(
            "blame", "--porcelain", f"-L{start},{end}", self.head, "--", repo_path(path)
        )
        return _porcelain(out)

    async def show(self, sha: str, path: str | None = None) -> str:
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
                    text=line[1:],
                )
            )  # fmt: skip
            continue
        head = line.split(" ")
        if len(head) >= 3 and _SHA.match(head[0]) and len(head[0]) == 40:
            sha, final = head[0], int(head[2])
            facts.setdefault(sha, {})
        elif sha and " " in line:
            key, value = line.split(" ", 1)
            facts[sha].setdefault(key, value)
    return found
