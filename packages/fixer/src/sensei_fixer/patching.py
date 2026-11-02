"""A fix's change in its checkout (8.7): written by the patch agent's tools only, read back as a
git diff, applied again on a clean checkout to verify it.

The diff is git's (safely configured, :func:`sensei_fixer.history.git`) of exactly the files the
tools wrote, with every file read as text (a repository's attributes can't hide a change as
"binary") and renames shown as a deletion and an addition, so the checks read every line. It
is what verification applies and what the pull request will hold.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sensei_engine.snapshots import UnsafePathError, check_path
from sensei_fixer.history import HistoryError, git

MAX_DIFF_BYTES = 512 * 1024
MAX_FILE_BYTES = 1024 * 1024  # a file the patch agent writes


class PatchError(Exception):
    """A change that can't be made or read (what, said for the model or for people)."""


@dataclass(frozen=True, slots=True)
class Diff:
    text: str
    files: tuple[str, ...]


def safe_path(root: Path, raw: object) -> Path:
    """``raw`` (a path in the repository) under ``root``, never through a symbolic link."""
    try:
        path = check_path(str(raw))
    except UnsafePathError:
        raise PatchError(f"{raw!r} isn't a path in the repository.") from None
    if not path or path.split("/", 1)[0] == ".git":
        raise PatchError(f"{raw!r} isn't a file of the repository.")
    target = root / path
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise PatchError(f"{path} goes through a symbolic link; it can't be written.")
    return target


def _write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


async def write_file(root: Path, path: str, content: str) -> None:
    if len(content.encode()) > MAX_FILE_BYTES:
        raise PatchError(f"A file of at most {MAX_FILE_BYTES} bytes.")
    await asyncio.to_thread(_write, safe_path(root, path), content)


async def read_text(root: Path, path: str) -> str | None:
    """``path``'s text in the checkout (None: there is no such file)."""
    target = safe_path(root, path)

    def read() -> str | None:
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    return await asyncio.to_thread(read)


async def diff_of(root: Path, paths: Iterable[str]) -> Diff:
    """The change to ``paths`` against the checkout's commit."""
    files = tuple(dict.fromkeys(check_path(p) for p in paths))
    if not files:
        return Diff("", ())
    try:
        await git(root, "add", "--force", "--all", "--", *files)
        text = await git(
            root, "-c", "core.quotePath=false", "diff", "--cached", "--no-renames", "--text",
            "--no-ext-diff", "--no-textconv", "--full-index", "--no-color", "HEAD", "--", *files,
            cap=MAX_DIFF_BYTES, whole=True,
        )  # fmt: skip
    except HistoryError as error:
        raise PatchError(f"The change can't be read: {error}") from None
    return Diff(text, files)


async def apply(root: Path, diff: str) -> None:
    """Apply ``diff`` to the clean checkout at ``root`` (all of it, or nothing)."""
    try:
        await git(root, "apply", "--whitespace=nowarn", "-", stdin=diff.encode())
    except HistoryError as error:
        raise PatchError(f"The patch doesn't apply: {error}") from None


async def restore(root: Path) -> None:
    """The checkout as its commit has it (what a stage changed, undone)."""
    try:
        await git(root, "reset", "--hard", "--quiet")
        await git(root, "clean", "-ffdxq")
    except HistoryError as error:
        raise PatchError(f"The checkout can't be restored: {error}") from None
