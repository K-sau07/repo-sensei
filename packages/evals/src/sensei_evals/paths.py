"""Where eval data lives. Everything under ``evals/private/`` stays on this machine (never
committed); paths are anchored at the repository root, so running from a subdirectory can't
scatter private files into tracked folders."""

from __future__ import annotations

import subprocess
from pathlib import Path


def repo_root(start: Path | None = None) -> Path:
    """The repo-sensei checkout containing ``start`` (default: the working directory)."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / "packages" / "evals" / "pyproject.toml").is_file():
            return directory
    msg = f"{here} is not inside a repo-sensei checkout"
    raise FileNotFoundError(msg)


def private_dir(root: Path) -> Path:
    return root / "evals" / "private"


def golden_dir(root: Path) -> Path:
    return private_dir(root) / "golden"


def reports_dir(root: Path) -> Path:
    return private_dir(root) / "reports"


def repos_file(root: Path) -> Path:
    return private_dir(root) / "repos.yaml"


def checkouts_dir(root: Path) -> Path:
    """Checkouts of the pinned benchmark repositories (``.cache/`` is ignored by git)."""
    return root / ".cache" / "eval-repos"


def suite_path(root: Path, suite: str) -> Path:
    """``suite`` as a path if it is one, else ``evals/private/golden/<suite>.yaml``."""
    candidate = Path(suite)
    if candidate.suffix in {".yaml", ".yml"} or candidate.is_file():
        return candidate
    return golden_dir(root) / f"{suite}.yaml"


def _git(*args: str, cwd: Path) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],  # noqa: S607 - git from PATH, like every developer tool here
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def head_sha(directory: Path) -> str | None:
    """The commit checked out in ``directory``, or None if it isn't the root of a git checkout
    (a plain directory inside another repository doesn't count as that repository's commit)."""
    top = _git("rev-parse", "--show-toplevel", cwd=directory)
    if top is None or Path(top).resolve() != directory.resolve():
        return None
    return _git("rev-parse", "HEAD", cwd=directory)


def has_uncommitted_changes(directory: Path) -> bool:
    return bool(_git("status", "--porcelain", "--untracked-files=no", cwd=directory))
