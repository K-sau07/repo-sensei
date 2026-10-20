"""Checking out a commit: exact commit, cache reuse, limits, and the token kept off disk and argv.

Uses a local bare repository (see conftest) through file://, so no network is involved.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sensei_core.config import IngestSettings
from sensei_workers.ingest.git import (
    ASKPASS_SCRIPT,
    CHECKED_OUT_MARKER,
    CloneError,
    CloneSource,
    GitRunner,
)

GIT = shutil.which("git") or "/usr/bin/git"
# Shaped like an installation token, but assembled here so no token-like literal is in the source
# (the secret scanner would rightly flag one).
TOKEN = "ghs_" + "FakeInstallationToken" + "0123456789abcdef"


@pytest.fixture
def settings(tmp_path: Path) -> IngestSettings:
    return IngestSettings(cache_dir=tmp_path / "cache", allow_file_protocol=True)


@pytest.fixture
def runner(settings: IngestSettings) -> Iterator[GitRunner]:
    git = GitRunner(settings)
    yield git
    git.close()


def _source(bare_repo: Any, token: str | None = None) -> CloneSource:
    return CloneSource(url=f"file://{bare_repo.path}", token=token)


def _deadline(seconds: float = 60) -> float:
    return time.monotonic() + seconds


def _head(path: Path) -> str:
    return subprocess.run(  # noqa: S603 - the system git, fixed arguments
        [GIT, "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_checks_out_exactly_the_commit(runner: GitRunner, bare_repo: Any, tmp_path: Path) -> None:
    dest = tmp_path / "copy"
    checkout = runner.checkout(_source(bare_repo), bare_repo.first, dest, deadline=_deadline())
    assert _head(dest) == bare_repo.first
    assert (dest / "src" / "app.py").read_text() == "print('hello')\n"
    assert (dest / "old.txt").exists()
    assert (checkout.commit_sha, checkout.files, checkout.submodules) == (bare_repo.first, 5, 0)
    assert checkout.bytes == sum(
        len(text)
        for text in (
            "# Demo\n",
            "print('hello')\n",
            "only in the first commit\n",
            "#!/bin/sh\necho run\n",
            "../../etc/passwd",  # a symlink's blob is its target
        )
    )
    assert (dest / "outside").is_symlink()


def test_the_cache_is_reused_and_cleaned(runner: GitRunner, bare_repo: Any, tmp_path: Path) -> None:
    dest = tmp_path / "copy"
    runner.checkout(_source(bare_repo), bare_repo.first, dest, deadline=_deadline())
    (dest / "leftover.log").write_text("from an earlier run")
    checkout = runner.checkout(_source(bare_repo), bare_repo.second, dest, deadline=_deadline())
    assert _head(dest) == bare_repo.second
    assert not (dest / "old.txt").exists()  # deleted in the second commit
    assert not (dest / "leftover.log").exists()  # untracked files are cleaned
    assert (dest / "src" / "app.py").read_text() == "print('hello, again')\n"
    assert checkout.submodules == 1
    assert not (dest / "vendor" / "lib" / "README.md").exists()  # submodules aren't fetched


def test_the_marker_names_the_commit_once_checked_out(
    runner: GitRunner, bare_repo: Any, tmp_path: Path
) -> None:
    dest = tmp_path / "copy"
    runner.checkout(_source(bare_repo), bare_repo.first, dest, deadline=_deadline())
    assert (dest / ".git" / CHECKED_OUT_MARKER).read_text() == bare_repo.first


def test_a_copy_left_broken_by_a_killed_run_recovers(
    runner: GitRunner, bare_repo: Any, tmp_path: Path
) -> None:
    dest = tmp_path / "copy"
    runner.checkout(_source(bare_repo), bare_repo.first, dest, deadline=_deadline())
    (dest / ".git" / "index.lock").write_text("")  # git was killed mid-write
    (dest / ".git" / "shallow.lock").write_text("")
    runner.checkout(_source(bare_repo), bare_repo.second, dest, deadline=_deadline())
    assert _head(dest) == bare_repo.second
    # Worse: the object store itself is broken. The copy is wiped and fetched again.
    for pack in (dest / ".git" / "objects").rglob("*"):
        if pack.is_file():
            pack.chmod(0o644)  # git writes objects read-only
            pack.write_bytes(b"garbage")
    runner.checkout(_source(bare_repo), bare_repo.first, dest, deadline=_deadline())
    assert _head(dest) == bare_repo.first
    assert (dest / ".git" / CHECKED_OUT_MARKER).read_text() == bare_repo.first


def test_too_many_files_are_refused_before_checkout(bare_repo: Any, tmp_path: Path) -> None:
    git = GitRunner(IngestSettings(cache_dir=tmp_path, allow_file_protocol=True, max_files=1))
    try:
        with pytest.raises(CloneError) as exc:
            git.checkout(_source(bare_repo), bare_repo.first, tmp_path / "c", deadline=_deadline())
    finally:
        git.close()
    assert exc.value.code == "too_many_files"


def test_lfs_files_stay_pointers(runner: GitRunner, bare_repo: Any, tmp_path: Path) -> None:
    dest = tmp_path / "copy"
    runner.checkout(_source(bare_repo), bare_repo.second, dest, deadline=_deadline())
    assert (dest / "model.bin").read_text().startswith("version https://git-lfs.github.com")


def test_oversized_repositories_are_refused_before_checkout(bare_repo: Any, tmp_path: Path) -> None:
    git = GitRunner(
        IngestSettings(cache_dir=tmp_path, allow_file_protocol=True, max_checkout_bytes=10)
    )
    dest = tmp_path / "copy"
    try:
        with pytest.raises(CloneError) as exc:
            git.checkout(_source(bare_repo), bare_repo.first, dest, deadline=_deadline())
    finally:
        git.close()
    assert exc.value.code == "repo_too_large"
    assert "10 bytes" in exc.value.message
    assert not dest.exists()  # nothing checked out, and what was fetched is gone too


def test_a_missing_commit_is_not_found(runner: GitRunner, bare_repo: Any, tmp_path: Path) -> None:
    with pytest.raises(CloneError) as exc:
        runner.checkout(_source(bare_repo), "e" * 40, tmp_path / "copy", deadline=_deadline())
    assert exc.value.code == "repo_not_found"


# ------------------------------------------------------------------ the token


def test_the_token_never_reaches_argv_or_disk(
    runner: GitRunner, bare_repo: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []
    real_popen = subprocess.Popen

    def spy(argv: list[str], **kwargs: Any) -> Any:
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    dest = tmp_path / "copy"
    runner.checkout(_source(bare_repo, TOKEN), bare_repo.second, dest, deadline=_deadline())

    assert seen
    assert all(TOKEN not in " ".join(argv) for argv, _ in seen)
    # Only the fetch gets it, and only in the environment the askpass helper reads.
    with_token = [argv for argv, env in seen if TOKEN in env.values()]
    assert len(with_token) == 1
    assert "fetch" in with_token[0]
    for _argv, env in seen:
        assert [k for k, v in env.items() if v == TOKEN] in ([], ["SENSEI_GIT_TOKEN"])
    # Nothing on disk holds it: not .git/config, not the objects, not the worktree.
    for path in dest.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert TOKEN.encode() not in path.read_bytes(), path
    assert TOKEN not in ASKPASS_SCRIPT


def test_askpass_answers_from_the_environment(runner: GitRunner, tmp_path: Path) -> None:
    script = tmp_path / "askpass.sh"
    script.write_text(ASKPASS_SCRIPT)
    script.chmod(0o700)
    env = {"PATH": "/usr/bin:/bin", "SENSEI_GIT_TOKEN": TOKEN}

    def ask(prompt: str) -> str:
        return subprocess.run(  # noqa: S603
            [str(script), prompt], env=env, check=True, capture_output=True, text=True
        ).stdout

    assert ask("Username for 'https://github.com': ") == "x-access-token\n"
    assert ask("Password for 'https://x-access-token@github.com': ") == f"{TOKEN}\n"


def test_git_gets_a_minimal_environment(runner: GitRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE", "GIT_SSH_COMMAND"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-pass-me")
    env = runner.environment(TOKEN)
    assert not [k for k in env if k.startswith(("GIT_TRACE", "GIT_CURL", "GIT_SSH", "AWS_"))]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert "SENSEI_GIT_TOKEN" not in runner.environment(None)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/demo.git",
        "ssh://git@github.com/acme/demo.git",
        "https://x-access-token:ghs_x@github.com/acme/demo.git",
        "ext::sh -c touch% /tmp/pwned",
    ],
)
def test_only_plain_https_remotes(url: str, tmp_path: Path, bare_repo: Any) -> None:
    git = GitRunner(IngestSettings(cache_dir=tmp_path))  # no file:// either
    try:
        with pytest.raises(CloneError) as exc:
            git.checkout(
                CloneSource(url=url), bare_repo.first, tmp_path / "c", deadline=_deadline()
            )
        with pytest.raises(CloneError):
            git.checkout(_source(bare_repo), bare_repo.first, tmp_path / "c", deadline=_deadline())
    finally:
        git.close()
    assert exc.value.code == "invalid_remote"


def test_slow_git_is_stopped_at_the_deadline(tmp_path: Path, bare_repo: Any) -> None:
    slow = tmp_path / "slow-git"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    git = GitRunner(
        IngestSettings(cache_dir=tmp_path, git_binary=str(slow), allow_file_protocol=True)
    )
    started = time.monotonic()
    try:
        with pytest.raises(CloneError) as exc:
            git.checkout(
                _source(bare_repo), bare_repo.first, tmp_path / "c", deadline=_deadline(0.5)
            )
    finally:
        git.close()
    assert exc.value.code == "clone_timeout"
    assert time.monotonic() - started < 5


def test_the_private_directory_is_removed(settings: IngestSettings) -> None:
    git = GitRunner(settings)
    private = git.environment()["GIT_ASKPASS"]
    git.close()
    assert not Path(private).exists()
