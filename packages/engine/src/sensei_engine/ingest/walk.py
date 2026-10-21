"""Walk a checked-out tree: the regular files worth indexing, in a stable order.

Symbolic links are never followed (a link can point anywhere on the worker's disk) and are left
out, as are sockets, devices and fifos, paths that aren't valid UTF-8, ``.git`` (whatever the
rules say) and everything the ignore rules skip (see :mod:`sensei_engine.ingest.ignore`).
Directories are listed in name order, so two walks of the same tree give the same list.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from sensei_engine.ingest.ignore import Attribute, IgnoreRules, Scope


class TooManyFilesError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"more than {limit} files")
        self.limit = limit


@dataclass(frozen=True, slots=True)
class WalkedFile:
    path: str  # relative to the root, POSIX separators
    absolute: Path
    size: int
    attributes: frozenset[Attribute]


@dataclass(slots=True)
class WalkStats:
    ignored_files: int = 0
    ignored_dirs: int = 0
    symlinks: int = 0
    special: int = 0  # sockets, devices, fifos
    bad_names: int = 0  # not valid UTF-8
    invalid_patterns: list[str] = field(default_factory=list[str])


def _valid_name(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:  # undecodable bytes surface as lone surrogates
        return False
    return "\n" not in name and "\r" not in name


def walk(root: Path, rules: IgnoreRules, *, max_files: int) -> tuple[list[WalkedFile], WalkStats]:
    """Every file under ``root`` that indexing keeps. Raises :class:`TooManyFilesError` past
    ``max_files``."""
    stats = WalkStats(invalid_patterns=list(rules.invalid_settings_globs))
    files: list[WalkedFile] = []
    pending: list[tuple[Path, str, Scope]] = [(root, "", rules.enter(Scope(), root, ""))]
    while pending:
        directory, relative, scope = pending.pop()
        with os.scandir(directory) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        subdirectories: list[tuple[Path, str, Scope]] = []
        for entry in entries:
            if not _valid_name(entry.name):
                stats.bad_names += 1
                continue
            if entry.name == ".git":
                continue  # repository internals, whatever any rule says
            path = f"{relative}/{entry.name}" if relative else entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                stats.symlinks += 1
            elif stat.S_ISDIR(info.st_mode):
                if rules.ignored(scope, path, is_dir=True):
                    stats.ignored_dirs += 1
                else:
                    child = Path(entry.path)
                    subdirectories.append((child, path, rules.enter(scope, child, path)))
            elif not stat.S_ISREG(info.st_mode):
                stats.special += 1
            elif rules.ignored(scope, path, is_dir=False):
                stats.ignored_files += 1
            else:
                if len(files) >= max_files:
                    raise TooManyFilesError(max_files)
                files.append(
                    WalkedFile(
                        path, Path(entry.path), info.st_size, IgnoreRules.attributes(scope, path)
                    )
                )
        pending.extend(reversed(subdirectories))  # depth first, in name order
    files.sort(key=lambda walked: walked.path)
    return files, stats
