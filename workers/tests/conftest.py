"""A local bare repository standing in for GitHub (no network): two commits, a submodule entry,
an LFS pointer, a symlink pointing outside the repository and an executable script; and a third
commit, on a branch of its own, that adds TypeScript for the graph's resolvers."""

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
# web/ in the third commit: a module that imports another's function and calls it.
WEB = {
    "web/greet.ts": "export function greet(name: string): string {\n  return `hello, ${name}`\n}\n",
    "web/main.ts": (
        'import { greet } from "./greet"\n\nexport function main(): void {\n  greet("again")\n}\n'
    ),
}

# stack/ in the fourth commit (the "stack" branch, from the second): a page's call, the FastAPI
# route it reaches, and the table its handler reads, for the links stage.
STACK = {
    "stack/api.py": (
        "from fastapi import FastAPI\n"
        "from sqlmodel import Field, Session, SQLModel, select\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        "\n"
        "class Item(SQLModel, table=True):\n"
        "    id: int | None = Field(default=None, primary_key=True)\n"
        "    title: str\n"
        "\n"
        "\n"
        '@app.get("/items")\n'
        "def read_items(session: Session):\n"
        "    return session.exec(select(Item)).all()\n"
    ),
    "stack/page.ts": (
        "export async function loadItems() {\n"
        '  const response = await fetch("/items")\n'
        "  return response.json()\n"
        "}\n"
    ),
}


@dataclass(frozen=True)
class BareRepo:
    path: Path  # .../<owner>/<name>.git
    web_root: Path  # what github.web_url points at (file://<web_root>)
    first: str  # commit ids
    second: str
    third: str  # the second and web/ (on the "web" branch: main stays at the second)
    app: str  # src/app.py in the second commit
    fourth: str  # the second and stack/ (on the "stack" branch)
    fifth: str = ""  # the third, web/greet.ts moved to client/greet.ts (the "moves" branch)
    sixth: str = ""  # the fifth, web/main.ts changed and README.md deleted
    seventh: str = ""  # the sixth, greet renamed hello (and its caller changed to match)
    eighth: str = ""  # the fourth, its route taken out (the page still fetches /items)


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


def _moves(work: Path) -> tuple[str, str, str]:
    """The "moves" branch, from the checked-out commit (the third): a file moved and nothing
    else, then one changed and one deleted, then a function renamed with its caller
    (incremental runs, 5.2 and 5.3). Its three commits."""
    _git("checkout", "--quiet", "-b", "moves", cwd=work)
    (work / "client").mkdir()
    _git("mv", "web/greet.ts", "client/greet.ts", cwd=work)
    _git("commit", "--quiet", "-m", "fifth", cwd=work)
    fifth = _git("rev-parse", "HEAD", cwd=work)
    (work / "web" / "main.ts").write_text(
        'import { greet } from "../client/greet"\n\nexport function main(): void {\n'
        '  greet("once more")\n}\n\nexport function quiet(): void {}\n'
    )
    (work / "README.md").unlink()
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "sixth", cwd=work)
    sixth = _git("rev-parse", "HEAD", cwd=work)
    (work / "client" / "greet.ts").write_text(
        "export function hello(name: string): string {\n  return `hello, ${name}`\n}\n"
    )
    (work / "web" / "main.ts").write_text(
        'import { hello } from "../client/greet"\n\nexport function main(): void {\n'
        '  hello("once more")\n}\n\nexport function quiet(): void {}\n'
    )
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "seventh", cwd=work)
    return fifth, sixth, _git("rev-parse", "HEAD", cwd=work)


def _unrouted(work: Path) -> str:
    """On the checked-out "stack" branch: its route taken out, its page left fetching it (a
    removed endpoint, 5.3). The commit."""
    api = work / "stack" / "api.py"
    api.write_text(api.read_text().replace('@app.get("/items")\n', ""))
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "eighth", cwd=work)
    eighth = _git("rev-parse", "HEAD", cwd=work)
    _git("checkout", "--quiet", "HEAD~1", cwd=work)  # the stack branch's head stays the fourth
    _git("branch", "--quiet", "-f", "stack", "HEAD", cwd=work)
    _git("branch", "--quiet", "unrouted", eighth, cwd=work)
    _git("checkout", "--quiet", "stack", cwd=work)
    return eighth


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
    _git("checkout", "--quiet", "-b", "web", cwd=work)
    (work / "web").mkdir()
    for path, content in WEB.items():
        (work / path).write_text(content)
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "third", cwd=work)
    third = _git("rev-parse", "HEAD", cwd=work)
    fifth, sixth, seventh = _moves(work)
    _git("checkout", "--quiet", "main", cwd=work)
    _git("checkout", "--quiet", "-b", "stack", cwd=work)
    (work / "stack").mkdir()
    for path, content in STACK.items():
        (work / path).write_text(content)
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "fourth", cwd=work)
    fourth = _git("rev-parse", "HEAD", cwd=work)
    eighth = _unrouted(work)
    _git("checkout", "--quiet", "main", cwd=work)

    web_root = root / "github"
    bare = web_root / "acme-gh" / "demo.git"
    bare.parent.mkdir(parents=True)
    _git("clone", "--quiet", "--bare", str(work), str(bare), cwd=root)
    # GitHub lets clients fetch any reachable commit by id; a local bare repo must be told to.
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    return BareRepo(
        path=bare,
        web_root=web_root,
        first=first,
        second=second,
        third=third,
        app=APP,
        fourth=fourth,
        fifth=fifth,
        sixth=sixth,
        seventh=seventh,
        eighth=eighth,
    )
