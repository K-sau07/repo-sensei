"""Discovery: every file in a checked-out tree that indexing keeps, and what each one is.

Two passes over the walked files: the manifests first (frameworks decide which directories are
frontend packages), then every file is read once, classified and given a role. File contents
aren't kept, so memory stays flat however large the repository is.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sensei_engine.ingest.classify import FileFacts, Flag, classify
from sensei_engine.ingest.frameworks import (
    Framework,
    detect_frameworks,
    framework_root,
    is_manifest,
)
from sensei_engine.ingest.ignore import IgnoreRules
from sensei_engine.ingest.languages import DISPLAY_NAMES
from sensei_engine.ingest.roles import UI_FRAMEWORKS, Role, assign_role
from sensei_engine.ingest.walk import WalkStats, walk

# Files left out of the per-language byte counts, as GitHub leaves them out of its language bar.
NOT_COUNTED = frozenset({Flag.BINARY, Flag.GENERATED, Flag.VENDORED, Flag.DOCUMENTATION})
PROGRESS_EVERY = 500  # files between progress callbacks


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: str
    size: int
    blob_sha: str
    content_hash: bytes
    fast_hash: bytes
    language: str | None
    loc: int | None
    role: Role
    flags: tuple[str, ...]  # sorted
    framework_root: str | None


@dataclass(frozen=True, slots=True)
class Discovery:
    files: list[DiscoveredFile]
    frameworks: list[Framework]
    stats: WalkStats

    @property
    def loc(self) -> int:
        return sum(file.loc or 0 for file in self.files)

    @property
    def bytes(self) -> int:
        return sum(file.size for file in self.files)

    def github_languages(self) -> dict[str, int]:
        """Bytes per language under GitHub's names, counted the way GitHub counts them."""
        counts: Counter[str] = Counter()
        for file in self.files:
            display = DISPLAY_NAMES.get(file.language or "")
            if display and file.role is not Role.DOCS and not NOT_COUNTED.intersection(file.flags):
                counts[display] += file.size
        return dict(counts.most_common())

    def summary(self) -> dict[str, Any]:
        languages: dict[str, Counter[str]] = defaultdict(Counter)
        roles: dict[str, Counter[str]] = defaultdict(Counter)
        flags: Counter[str] = Counter()
        for file in self.files:
            for totals in (languages[file.language or "unknown"], roles[file.role.value]):
                totals.update(files=1, loc=file.loc or 0, bytes=file.size)
            flags.update(file.flags)
        stats = self.stats
        return {
            "files": len(self.files),
            "loc": self.loc,
            "bytes": self.bytes,
            "languages": {k: dict(v) for k, v in sorted(languages.items())},
            "roles": {k: dict(v) for k, v in sorted(roles.items())},
            "flags": dict(sorted(flags.items())),
            "frameworks": [framework.as_json() for framework in self.frameworks],
            "skipped": {
                "ignored_files": stats.ignored_files,
                "ignored_dirs": stats.ignored_dirs,
                "symlinks": stats.symlinks,
                "special": stats.special,
                "bad_names": stats.bad_names,
            },
            "invalid_ignore_globs": stats.invalid_patterns,
        }


def discover(
    root: Path,
    *,
    ignore_globs: Sequence[str] = (),
    max_files: int,
    progress: Callable[[int, int], None] | None = None,
) -> Discovery:
    """Discover the files under ``root``. ``progress(done, total)`` is called as files are read.
    Raises :class:`~sensei_engine.ingest.walk.TooManyFilesError` past ``max_files``."""
    walked, stats = walk(root, IgnoreRules(ignore_globs), max_files=max_files)
    manifests: dict[str, FileFacts] = {
        entry.path: classify(entry) for entry in walked if is_manifest(entry.path)
    }
    frameworks = detect_frameworks(
        {path: facts.text for path, facts in manifests.items() if facts.text is not None}
    )
    roots = sorted({framework.root for framework in frameworks})
    ui_roots = {f.root for f in frameworks if f.name in UI_FRAMEWORKS}

    files: list[DiscoveredFile] = []
    total = len(walked)
    for done, entry in enumerate(walked, start=1):
        facts = manifests.get(entry.path) or classify(entry)
        package = framework_root(entry.path, roots)
        role = assign_role(facts.path, facts.language, facts.text, ui_package=package in ui_roots)
        files.append(
            DiscoveredFile(
                path=facts.path,
                size=facts.size,
                blob_sha=facts.blob_sha,
                content_hash=facts.content_hash,
                fast_hash=facts.fast_hash,
                language=facts.language,
                loc=facts.loc,
                role=role,
                flags=tuple(sorted(flag.value for flag in facts.flags)),
                framework_root=package,
            )
        )
        if progress is not None and (done % PROGRESS_EVERY == 0 or done == total):
            progress(done, total)
    return Discovery(files=files, frameworks=frameworks, stats=stats)
