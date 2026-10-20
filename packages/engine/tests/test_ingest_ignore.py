"""Ignore rules: built-ins, nested .gitignore precedence, .senseiignore, settings globs and the
linguist attributes; plus the walk itself (no symlinks, stable order, file limit)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sensei_engine.ingest.ignore import Attribute, IgnoreRules
from sensei_engine.ingest.walk import TooManyFilesError, walk


def _tree(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def _paths(root: Path, globs: tuple[str, ...] = (), max_files: int = 1000) -> list[str]:
    files, _ = walk(root, IgnoreRules(globs), max_files=max_files)
    return [f.path for f in files]


def test_builtins_skip_dependencies_build_output_and_lockfiles(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "src/app.py": "",
            "node_modules/react/index.js": "",
            "web/node_modules/x/y.js": "",
            "dist/bundle.js": "",
            "src/__pycache__/app.cpython-313.pyc": "",
            "app.min.js": "",
            "app.js.map": "",
            "uv.lock": "",
            "pnpm-lock.yaml": "",
            "package-lock.json": "",
            "Cargo.lock": "",
            ".venv/lib/site.py": "",
            "vendor/github.com/pkg/x.go": "",
            "build.sh": "",  # a file named like a denied directory stays
        },
    )
    assert _paths(root) == ["build.sh", "src/app.py"]


def test_nested_gitignore_takes_precedence(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            ".gitignore": "*.log\n/root-only.txt\n",
            "root-only.txt": "",
            "a.log": "",
            "pkg/.gitignore": "!keep.log\nlocal/\n",
            "pkg/keep.log": "",  # re-included by the deeper file
            "pkg/other.log": "",  # still ignored by the root file
            "pkg/root-only.txt": "",  # anchored at the root: not this one
            "pkg/local/x.py": "",
            "pkg/deeper/.gitignore": "*.log\n",
            "pkg/deeper/keep.log": "",  # the deepest file decides again
        },
    )
    assert _paths(root) == [
        ".gitignore",
        "pkg/.gitignore",
        "pkg/deeper/.gitignore",
        "pkg/keep.log",
        "pkg/root-only.txt",
    ]


def test_an_ignored_directory_cannot_be_re_included(tmp_path: Path) -> None:
    root = _tree(tmp_path, {".gitignore": "out/\n!out/keep.txt\n", "out/keep.txt": ""})
    assert _paths(root) == [".gitignore"]


def test_senseiignore_overrides_gitignore_and_builtins(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            ".gitignore": "generated/\n",
            ".senseiignore": "!generated/\n!vendor/\nfixtures/\n",
            "generated/api.py": "",
            "vendor/lib.go": "",
            "fixtures/big.json": "",
        },
    )
    assert _paths(root) == [".gitignore", ".senseiignore", "generated/api.py", "vendor/lib.go"]


def test_settings_globs_are_strongest(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {".senseiignore": "!docs/\n", "docs/a.md": "", "src/a.py": "", "src/a_test.py": ""},
    )
    assert _paths(root, ("docs/", "*_test.py")) == [".senseiignore", "src/a.py"]


def test_invalid_patterns_are_skipped_and_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"a.py": "", "b.py": ""})
    rules = IgnoreRules(["b.py", "!"])
    files, stats = walk(root, rules, max_files=10)
    assert [f.path for f in files] == ["a.py"]
    assert stats.invalid_patterns == ["!"]


def test_linguist_attributes(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            ".gitattributes": "*.pb.go linguist-generated\nthird/** linguist-vendored\n"
            "docs/** linguist-documentation\nsrc/keep.pb.go -linguist-generated\n",
            "api.pb.go": "",
            "src/keep.pb.go": "",
            "third/lib.c": "",
            "docs/guide.md": "",
            "sub/.gitattributes": "*.py linguist-generated=true\n",
            "sub/x.py": "",
        },
    )
    files, _ = walk(root, IgnoreRules(), max_files=100)
    attributes = {f.path: f.attributes for f in files}
    assert attributes["api.pb.go"] == {Attribute.GENERATED}
    assert attributes["src/keep.pb.go"] == frozenset()
    assert attributes["third/lib.c"] == {Attribute.VENDORED}
    assert attributes["docs/guide.md"] == {Attribute.DOCUMENTATION}
    assert attributes["sub/x.py"] == {Attribute.GENERATED}


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = _tree(tmp_path / "outside", {"secret.txt": "s3cret", "dir/inner.txt": ""})
    root = _tree(tmp_path / "repo", {"a.py": ""})
    (root / "link.txt").symlink_to(outside / "secret.txt")
    (root / "linkdir").symlink_to(outside / "dir", target_is_directory=True)
    (root / ".gitignore").symlink_to(outside / "secret.txt")  # not read as rules either
    files, stats = walk(root, IgnoreRules(), max_files=100)
    assert [f.path for f in files] == ["a.py"]
    assert stats.symlinks == 3


def test_fifos_are_skipped(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"a.py": ""})
    os.mkfifo(root / "pipe")
    files, stats = walk(root, IgnoreRules(), max_files=100)
    assert [f.path for f in files] == ["a.py"]
    assert stats.special == 1


def test_names_that_are_not_utf8_are_skipped(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"a.py": ""})
    try:
        (root / os.fsdecode(b"bad-\xff.txt")).write_text("")
    except OSError:
        pytest.skip("this filesystem only allows UTF-8 names (macOS); Linux workers don't")
    files, stats = walk(root, IgnoreRules(), max_files=100)
    assert [f.path for f in files] == ["a.py"]
    assert stats.bad_names == 1


def test_the_order_is_stable(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"b/z.py": "", "b/a.py": "", "a.py": "", "a/b.py": "", "c.py": ""})
    assert _paths(root) == sorted(_paths(root)) == ["a.py", "a/b.py", "b/a.py", "b/z.py", "c.py"]


def test_too_many_files(tmp_path: Path) -> None:
    root = _tree(tmp_path, {f"f{i}.py": "" for i in range(5)})
    assert len(_paths(root, max_files=5)) == 5
    with pytest.raises(TooManyFilesError):
        _paths(root, max_files=4)


# ------------------------------------------------------------------ git's own answer

GIT = shutil.which("git")


def _git_keeps(root: Path) -> list[str]:
    """What git itself leaves unignored in ``root`` (no repository config, no global excludes)."""
    git_binary = GIT
    assert git_binary is not None
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - the system git, fixed arguments
            [git_binary, *args], cwd=root, env=env, check=True, capture_output=True, text=True
        ).stdout

    git("init", "--quiet")
    listed = git("ls-files", "--others", "--exclude-standard").splitlines()
    shutil.rmtree(root / ".git")
    return sorted(listed)


@pytest.mark.skipif(GIT is None, reason="needs git")
@pytest.mark.parametrize(
    "files",
    [
        # The whitelist idiom: ignore everything, then bring back directories and Python files.
        {
            ".gitignore": "*\n!*/\n!*.py\n!.gitignore\n",
            "a.py": "",
            "b.txt": "",
            "src/b.py": "",
            "src/c.txt": "",
            "src/deep/d.py": "",
        },
        # "a/**" is what is inside a, so a/b can be re-included.
        {".gitignore": "a/**\n!a/b\n", "a/b": "", "a/c": "", "a/d/e": "", "keep": ""},
        # A name without a slash matches files and directories at any depth.
        {".gitignore": "logs\n", "logs/x": "", "y/logs": "", "z/logs/w": "", "z/ok": ""},
        # A pattern with a slash in it is relative to its file's directory.
        {".gitignore": "doc/*.txt\n", "doc/a.txt": "", "doc/sub/b.txt": "", "x/doc/c.txt": ""},
        {".gitignore": "**/foo/bar\n", "foo/bar": "", "x/foo/bar": "", "x/foo/baz": ""},
        # Files ignored by name, one directory re-included by a deeper file.
        {
            ".gitignore": "*.log\n",
            "b.log": "",
            "important/.gitignore": "!*.log\n",
            "important/a.log": "",
            "important/sub/c.log": "",
        },
        # Re-including a directory doesn't re-include files ignored by name inside it.
        {".gitignore": "*.log\nlogs/\n!logs/\n", "logs/a.log": "", "logs/a.txt": ""},
    ],
)
def test_matches_what_git_ignores(tmp_path: Path, files: dict[str, str]) -> None:
    root = _tree(tmp_path, files)
    assert _paths(root) == _git_keeps(root)


def test_re_including_a_directory_keeps_the_builtins_inside_it(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            ".gitignore": "!build/\n",
            "build/app.py": "",
            "build/node_modules/x/index.js": "",
            "build/__pycache__/m.pyc": "",
            "build/bundle.min.js": "",
        },
    )
    assert _paths(root) == [".gitignore", "build/app.py"]


def test_git_internals_are_never_walked(tmp_path: Path) -> None:
    root = _tree(tmp_path, {".gitignore": "!.git/\n", ".git/config": "", "a.py": ""})
    rules = IgnoreRules(["!.git/", "!.git"])
    files, _ = walk(root, rules, max_files=10)
    assert [f.path for f in files] == [".gitignore", "a.py"]


def test_a_byte_order_mark_does_not_eat_the_first_pattern(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"a.log": "", "b.py": ""})
    (root / ".gitignore").write_bytes(b"\xef\xbb\xbf*.log\n")
    assert _paths(root) == [".gitignore", "b.py"]


def test_attributes_never_apply_through_directory_patterns(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {".gitattributes": "gen/ linguist-generated\ngen/** linguist-vendored\n", "gen/a.py": ""},
    )
    files, _ = walk(root, IgnoreRules(), max_files=10)
    # As `git check-attr`: "gen/" sets nothing on gen/a.py, "gen/**" does.
    assert {f.path: f.attributes for f in files}["gen/a.py"] == {Attribute.VENDORED}
