"""The snapshot archive: a working copy as ``tar.zst``, without ``.git``.

Entries are sorted and normalised (owner 0, mtime 0, 644/755 permissions), so the same commit
gives the same bytes. Symlinks are stored as links and never followed, so a link pointing outside
the repository can't pull other files in; anything that isn't a file, directory or link (devices,
sockets) is left out.
Unpacking lives in :mod:`sensei_engine.snapshots` (the API reads snapshots too).
"""

from __future__ import annotations

import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path

import zstandard

ZSTD_LEVEL = 10


@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    path: Path
    bytes: int  # compressed size
    entries: int


def _normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & stat.S_IXUSR else 0o644
    elif info.issym():
        info.mode = 0o777  # macOS reports 0755 for links, Linux 0777: the same bytes everywhere
    return info


def _entries(worktree: Path) -> list[Path]:
    found: list[Path] = []
    for root, dirs, files in os.walk(worktree, followlinks=False):
        here = Path(root)
        names = files
        if here == worktree:
            dirs[:] = [d for d in dirs if d != ".git"]
            names = [f for f in files if f != ".git"]  # a .git file (worktrees) too
        dirs.sort()
        found.extend(here / name for name in dirs)
        found.extend(here / name for name in sorted(names))
    return sorted(found, key=lambda path: path.relative_to(worktree).as_posix())


def write_archive(worktree: Path, out: Path) -> ArchiveInfo:
    entries = 0
    compressor = zstandard.ZstdCompressor(level=ZSTD_LEVEL, threads=-1)
    with (
        out.open("wb") as raw,
        compressor.stream_writer(raw, closefd=False) as zst,
        tarfile.open(fileobj=zst, mode="w|", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in _entries(worktree):
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                continue
            arcname = path.relative_to(worktree).as_posix()
            tar.add(path, arcname=arcname, recursive=False, filter=_normalise)
            entries += 1
    return ArchiveInfo(path=out, bytes=out.stat().st_size, entries=entries)
