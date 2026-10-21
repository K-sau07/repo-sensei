"""Snapshots on local disk: unpacking an archive safely, and a cache of unpacked snapshots.

Unpacking applies the standard library's ``data`` filter: nothing lands outside the destination
(no absolute paths, no ``..``, no links pointing out, no device files); entries that would are
skipped. :class:`SnapshotCache` keeps unpacked snapshots for serving files (the API has no
working copies of its own), least recently used out first, within a disk budget. A snapshot is
unpacked into a scratch directory and renamed into place, so readers only ever see whole ones,
under a per-snapshot ``flock`` so the API's worker processes don't unpack it twice.

Files are read with :func:`read_file`: the path must be relative and normal, every component is
opened without following symbolic links, and reads are bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import BinaryIO

import zstandard

READY_MARKER = ".sensei-ready"  # inside an unpacked snapshot: written last, touched on use
KEEP_FRACTION = 0.8
SCRATCH_PREFIX = ".unpack-"
LOCKS = ".locks"


class ArchiveTooLargeError(Exception):
    """The archive unpacks to more than allowed."""


class UnsafePathError(ValueError):
    """Not a relative, normal path inside the snapshot."""


def extract_archive(archive: Path, dest: Path, *, max_bytes: int) -> int:
    """Unpack ``archive`` into ``dest`` (created; must not exist). Returns the entries written;
    raises :class:`ArchiveTooLargeError` once the files add up to more than ``max_bytes``."""
    dest.mkdir(parents=True)
    target = str(dest)
    unpacked = 0
    written: set[str] = set()

    def safe(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        nonlocal unpacked
        try:
            checked = tarfile.data_filter(member, path)
        except tarfile.FilterError:
            return None  # would land outside dest: skip it
        if checked.name not in written:  # directories come through twice (again at the end)
            written.add(checked.name)
            unpacked += checked.size
            if unpacked > max_bytes:
                raise ArchiveTooLargeError(f"over {max_bytes} bytes unpacked")
        return checked

    decompressor = zstandard.ZstdDecompressor()
    with (
        archive.open("rb") as raw,
        decompressor.stream_reader(raw, closefd=False) as zst,
        tarfile.open(fileobj=zst, mode="r|") as tar,
    ):
        tar.extractall(target, filter=safe)  # noqa: S202 - every member passes the data filter
    return len(written)


def check_path(path: str) -> str:
    """``path`` as stored in ``files.path`` (relative; a trailing slash is dropped), or
    :class:`UnsafePathError` for absolute paths, ``.``/``..``/empty segments, backslashes and
    NUL. The empty string (or "/") is the root."""
    if path in ("", "/"):
        return ""
    if path.startswith("/"):
        raise UnsafePathError(path)
    cleaned = path.rstrip("/")
    parts = cleaned.split("/")
    if (
        "\\" in cleaned
        or "\0" in cleaned
        or any(part in ("", ".", "..") for part in parts)
        or len(cleaned) > 4096
    ):
        raise UnsafePathError(path)
    return cleaned


def open_file(root: Path, path: str) -> BinaryIO:
    """``path`` under ``root``, opened for reading: each directory and the file itself without
    following symbolic links, and only if it is a regular file (else ``FileNotFoundError``)."""
    parts = check_path(path).split("/")
    if parts == [""]:
        raise UnsafePathError(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory = os.open(root, flags | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | getattr(os, "O_DIRECTORY", 0), dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], flags, dir_fd=directory)
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise FileNotFoundError(path)
    return os.fdopen(fd, "rb")


def read_file(root: Path, path: str, *, max_bytes: int) -> bytes:
    """The bytes of ``path`` under ``root`` (at most ``max_bytes``; more raises ValueError),
    opened as :func:`open_file` does."""
    with open_file(root, path) as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        msg = f"{path} is larger than {max_bytes} bytes"
        raise ValueError(msg)
    return data


def _size(path: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(base) / name).lstat().st_size
    return total


Download = Callable[[Path], Awaitable[None]]


class SnapshotCache:
    def __init__(self, root: Path, *, max_bytes: int, max_snapshot_bytes: int) -> None:
        self._root = root
        self._max_bytes = max_bytes
        self._max_snapshot_bytes = max_snapshot_bytes
        self._locks: dict[str, asyncio.Lock] = {}

    async def unpacked(self, snapshot_id: uuid.UUID, download: Download) -> Path:
        """The snapshot's files on disk; ``download(path)`` writes its archive to ``path`` when
        it isn't here yet."""
        name = str(snapshot_id)
        here = self._root / name
        if (here / READY_MARKER).is_file():
            (here / READY_MARKER).touch()
            return here
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock, self._process_lock(name):
            if (here / READY_MARKER).is_file():
                return here
            scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=self._root))
            try:
                archive = scratch / "snapshot.tar.zst"
                await download(archive)
                tree = scratch / "tree"
                await asyncio.to_thread(
                    extract_archive, archive, tree, max_bytes=self._max_snapshot_bytes
                )
                (tree / READY_MARKER).write_text(str(time.time()))
                shutil.rmtree(here, ignore_errors=True)  # a half-unpacked leftover
                tree.rename(here)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            await asyncio.to_thread(self._evict, keep=name)
            return here

    @contextlib.asynccontextmanager
    async def _process_lock(self, name: str) -> AsyncGenerator[None]:
        """Other processes on this machine (API workers) unpack the same snapshot only once."""
        locks = self._root / LOCKS
        locks.mkdir(parents=True, exist_ok=True)
        fd = os.open(locks / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # releases the lock

    def _evict(self, *, keep: str) -> None:
        entries: list[tuple[float, int, Path]] = []
        for entry in self._root.iterdir():
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            marker = entry / READY_MARKER
            try:
                used = marker.stat().st_mtime
            except OSError:
                used = 0.0
            entries.append((used, _size(entry), entry))
        total = sum(size for _, size, _ in entries)
        if total <= self._max_bytes:
            return
        for _used, size, path in sorted(entries):
            if total <= self._max_bytes * KEEP_FRACTION:
                break
            if path.name == keep:
                continue
            shutil.rmtree(path, ignore_errors=True)
            total -= size
