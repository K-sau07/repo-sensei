"""A throwaway copy of the bench's app under git, to plant a bug in and apply a fix to.

The copy leaves out what is installed or built (``node_modules``, virtualenvs, caches); a test
runner links the installed dependencies back in. Git runs with a fixed argv, never a shell, and
without the user's configuration (no hooks, no signing, a fixed author).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

SKIPPED = ("node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".next", ".cache")
GIT_TIMEOUT_SECONDS = 60


class PatchError(Exception):
    """A patch didn't apply (what git said)."""


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "TMPDIR", "LANG")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return env


class Workspace:
    """The app at ``path``, a git repository with the clean app as its first commit."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str, stdin: str | None = None) -> str:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [  # noqa: S607 - git from PATH
                "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "-c", "user.name=fixbench", "-c", "user.email=fixbench@localhost", *args,
            ],
            cwd=self.path,
            input=stdin,
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )  # fmt: skip
        if result.returncode != 0:
            raise PatchError((result.stderr or result.stdout).strip()[-2000:])
        return result.stdout

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def apply(self, patch: str, *, reverse: bool = False) -> None:
        """Apply a unified diff (``git apply``), or undo it."""
        if not patch.strip():
            raise PatchError("the patch is empty")
        args = ["apply", "--whitespace=nowarn", *(["--reverse"] if reverse else []), "-"]
        self.git(*args, stdin=patch)

    def diff(self, since: str) -> str:
        """What changed since ``since``, new files included, as a patch."""
        self.git("add", "-A")
        return self.git("diff", "--cached", "--binary", since)

    def inside(self, relative: str) -> Path:
        """``relative`` in the workspace, refusing a path that leads out of it (a symbolic
        link a diff made): the judge writes and deletes on this machine."""
        root = self.path.resolve()
        target = (self.path / relative).resolve()
        if not target.is_relative_to(root):
            raise PatchError(f"{relative} leads outside the workspace")
        return target

    def write(self, relative: str, content: str) -> None:
        target = self.inside(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise PatchError(f"{relative} is a symbolic link")
        target.write_text(content, encoding="utf-8")

    def remove(self, relative: str) -> None:
        target = self.inside(relative)
        target.unlink(missing_ok=True)


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in SKIPPED}


@contextmanager
def workspace(app: Path) -> Generator[Workspace]:
    """A fresh copy of ``app``, committed, removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="fixbench-") as temp:
        path = Path(temp) / "app"
        shutil.copytree(app, path, ignore=_ignore, symlinks=False)
        found = Workspace(path)
        found.git("init", "-q", "-b", "main")
        found.commit("clean")
        yield found
