"""The working-copy cache: one lock per repository, and staying under the disk budget.

Each repository's copy is used under an exclusive ``flock`` on ``.locks/<repo>``: the clone, the
stages that read the copy and eviction take it, so a copy is never deleted or rewritten while
another task on this machine is using it. The kernel drops the lock when a process dies, so a
killed task never leaves it stuck. Eviction runs on every machine that clones (each has its own
disk), least recently used first, and skips copies that are locked or used in the last hour.
Names starting with a dot are bookkeeping (locks, archives being written, unpacking scratch),
not copies; stale scratch is removed.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from sensei_workers.ingest.git import LAST_USED_MARKER

KEEP_FRACTION = 0.9  # evict down to 90% of the budget, so it doesn't run on every write
MIN_IDLE_SECONDS = 3600  # never touch a copy used within the last hour
SCRATCH_MAX_AGE_SECONDS = 86_400  # unpacking scratch and partial archives older than this go
LOCKS = ".locks"


class CacheBusyError(Exception):
    """Another task on this machine holds the repository's copy."""


@contextlib.contextmanager
def repo_lock(cache_dir: Path, name: str, *, wait: bool = True) -> Generator[None]:
    """Hold the exclusive lock on the copy ``name``. With ``wait=False``, raise
    :class:`CacheBusyError` at once if someone else has it."""
    locks = cache_dir / LOCKS
    locks.mkdir(parents=True, exist_ok=True)
    fd = os.open(locks / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise CacheBusyError(name) from None
        yield
    finally:
        os.close(fd)  # releases the lock


@dataclass(frozen=True, slots=True)
class CachedCopy:
    path: Path
    bytes: int
    last_used: float


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):  # removed while we looked
                total += (Path(root) / name).lstat().st_size
    return total


def _last_used(path: Path) -> float:
    marker = path / ".git" / LAST_USED_MARKER
    try:
        return float(marker.read_text().strip())
    except (OSError, ValueError):
        return path.stat().st_mtime


def cached_copies(cache_dir: Path) -> list[CachedCopy]:
    if not cache_dir.is_dir():
        return []
    copies = [
        CachedCopy(entry, _size(entry), _last_used(entry))
        for entry in cache_dir.iterdir()
        if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith(".")
    ]
    return sorted(copies, key=lambda copy: copy.last_used)


def evict(
    cache_dir: Path, max_bytes: int, *, keep: set[str], now: float | None = None
) -> list[Path]:
    """Remove the least recently used copies until the cache fits ``max_bytes`` (with margin).
    Copies named in ``keep`` (repositories being indexed) and recently used ones stay."""
    now = time.time() if now is None else now
    _remove_stale_scratch(cache_dir, now)
    copies = cached_copies(cache_dir)
    total = sum(copy.bytes for copy in copies)
    if total <= max_bytes:
        return []
    removed: list[Path] = []
    for copy in copies:
        if total <= max_bytes * KEEP_FRACTION:
            break
        if copy.path.name in keep or now - copy.last_used < MIN_IDLE_SECONDS:
            continue
        try:
            with repo_lock(cache_dir, copy.path.name, wait=False):
                shutil.rmtree(copy.path, ignore_errors=True)
        except CacheBusyError:
            continue  # in use on this machine right now
        total -= copy.bytes
        removed.append(copy.path)
    return removed


def _remove_stale_scratch(cache_dir: Path, now: float) -> None:
    """Unpacking scratch and archives that a killed task left behind."""
    if not cache_dir.is_dir():
        return
    scratch = [p for p in cache_dir.glob(".unpack-*") if p.is_dir() and not p.is_symlink()]
    archives = cache_dir / ".archives"
    if archives.is_dir():
        scratch += [p for p in archives.iterdir() if p.is_file() and not p.is_symlink()]
    for path in scratch:
        with contextlib.suppress(OSError):
            if now - path.lstat().st_mtime > SCRATCH_MAX_AGE_SECONDS:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
