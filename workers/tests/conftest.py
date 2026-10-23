"""A local bare repository standing in for GitHub (no network): two commits, a submodule entry,
an LFS pointer, a symlink pointing outside the repository and an executable script."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

GIT = shutil.which("git") or "/usr/bin/git"

LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
    "size 12345\n"
)
# src/app.py in the second commit: definitions for the symbols stage.
APP = (
    "class Greeter:\n"
    "    def greet(self, name):\n"
    "        print(f'hello, {name}')\n"
    "\n"
    "\n"
    "def main():\n"
    "    Greeter().greet('again')\n"
)


@dataclass(frozen=True)
class BareRepo:
    path: Path  # .../<owner>/<name>.git
    web_root: Path  # what github.web_url points at (file://<web_root>)
    first: str  # commit ids
    second: str
    app: str  # src/app.py in the second commit


def _git(*args: str, cwd: Path) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
    }
    result = subprocess.run(  # noqa: S603 - test fixture: the system git, fixed arguments
        [GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture(scope="session")
def bare_repo(tmp_path_factory: pytest.TempPathFactory) -> BareRepo:
    root = tmp_path_factory.mktemp("git")
    work = root / "work"
    work.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=work)
    (work / "README.md").write_text("# Demo\n")
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("print('hello')\n")
    (work / "old.txt").write_text("only in the first commit\n")
    script = work / "run.sh"
    script.write_text("#!/bin/sh\necho run\n")
    script.chmod(0o755)
    (work / "outside").symlink_to("../../etc/passwd")
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "first", cwd=work)
    first = _git("rev-parse", "HEAD", cwd=work)

    (work / "old.txt").unlink()
    (work / "src" / "app.py").write_text(APP)
    (work / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (work / "model.bin").write_text(LFS_POINTER)
    _git("add", "-A", cwd=work)
    # A submodule entry (gitlink) without the submodule itself.
    _git("update-index", "--add", "--cacheinfo", f"160000,{first},vendor/lib", cwd=work)
    _git("commit", "--quiet", "-m", "second", cwd=work)
    second = _git("rev-parse", "HEAD", cwd=work)

    web_root = root / "github"
    bare = web_root / "acme-gh" / "demo.git"
    bare.parent.mkdir(parents=True)
    _git("clone", "--quiet", "--bare", str(work), str(bare), cwd=root)
    # GitHub lets clients fetch any reachable commit by id; a local bare repo must be told to.
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    return BareRepo(path=bare, web_root=web_root, first=first, second=second, app=APP)
