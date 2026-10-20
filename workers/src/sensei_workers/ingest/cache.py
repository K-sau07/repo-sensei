"""Keeping the working-copy cache under its disk budget (least recently used first)."""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from sensei_workers.ingest.git import LAST_USED_MARKER

KEEP_FRACTION = 0.9  # evict down to 90% of the budget, so it doesn't run on every write
MIN_IDLE_SECONDS = 3600  # never touch a copy used within the last hour


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
        if entry.is_dir() and not entry.is_symlink()
    ]
    return sorted(copies, key=lambda copy: copy.last_used)


def evict(
    cache_dir: Path, max_bytes: int, *, keep: set[str], now: float | None = None
) -> list[Path]:
    """Remove the least recently used copies until the cache fits ``max_bytes`` (with margin).
    Copies named in ``keep`` (repositories being indexed) and recently used ones stay."""
    now = time.time() if now is None else now
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
        shutil.rmtree(copy.path, ignore_errors=True)
        total -= copy.bytes
        removed.append(copy.path)
    return removed
