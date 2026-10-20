"""Checking out one commit of a repository, safely.

How a snapshot is fetched: a working copy per repository (reused between runs), then
``git fetch --depth=1 origin <sha>`` for exactly the commit to index, a size check from the tree
listing *before* anything is written out, and ``git checkout --detach``. One shallow fetch
downloads only that commit's tree, where a partial clone would also bring every commit and tree
in the history.

Credentials: the installation token reaches git only through a ``GIT_ASKPASS`` script that reads
it from the git process's environment. It is never in argv (visible in ``ps``), in logs, or in
``.git/config``; remote URLs carry no userinfo. Git runs with a minimal environment (no system or
global config, no credential helpers, no ``GIT_TRACE*``, no prompts) and only the HTTPS protocol
(plus ``file://`` when explicitly allowed for local fixtures).

LFS files stay as pointer files (smudge is skipped) and submodules aren't fetched; both are
reported so the UI can say so.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sensei_core.config import IngestSettings
from sensei_core.redaction import redact_text

ASKPASS_SCRIPT = """#!/bin/sh
# git asks for a username, then a password; answer from the environment, never from disk.
case "$1" in
  Username*) printf '%s\\n' "x-access-token" ;;
  *) printf '%s\\n' "$SENSEI_GIT_TOKEN" ;;
esac
"""
LAST_USED_MARKER = "sensei-last-used"  # inside .git, so it never reaches the archive
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SUBMODULE_MODE = "160000"
_NOT_FOUND = ("repository not found", "not found", "does not appear to be a git repository")
_AUTH = ("authentication failed", "could not read username", "terminal prompts disabled")
_TRANSIENT = (
    "could not resolve host",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "the requested url returned error: 5",
    "early eof",
    "remote end hung up",
    "rpc failed",
)


class CloneError(Exception):
    """A clone that can't succeed as asked; ``code`` and the message are safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TransientGitError(Exception):
    """Network trouble talking to the git server: worth trying again later."""


@dataclass(frozen=True, slots=True)
class CloneSource:
    url: str  # no credentials in it
    token: str | None = None


@dataclass(frozen=True, slots=True)
class Checkout:
    path: Path
    commit_sha: str
    bytes: int  # checked-out size, from the tree listing
    files: int
    submodules: int  # not fetched


class GitRunner:
    def __init__(self, settings: IngestSettings) -> None:
        self._settings = settings
        self._private = Path(tempfile.mkdtemp(prefix="sensei-git-"))
        self._private.chmod(0o700)
        self._askpass = self._private / "askpass.sh"
        self._askpass.write_text(ASKPASS_SCRIPT)
        self._askpass.chmod(0o700)
        self._home = self._private / "home"  # empty: no ~/.gitconfig, no stored credentials
        self._home.mkdir(mode=0o700)

    # ------------------------------------------------------------------ the checkout

    def checkout(
        self, source: CloneSource, commit_sha: str, dest: Path, *, deadline: float
    ) -> Checkout:
        """``dest`` at exactly ``commit_sha`` (and nothing else: earlier files are cleaned)."""
        if not _SHA.match(commit_sha):
            raise CloneError("invalid_commit", f"Not a commit id: {commit_sha!r}.")
        self._check_url(source.url)
        dest.mkdir(parents=True, exist_ok=True)
        if not (dest / ".git").is_dir():
            self.run(["init", "--quiet"], cwd=dest, deadline=deadline)
        self.run(["config", "remote.origin.url", source.url], cwd=dest, deadline=deadline)
        self._fetch(source, commit_sha, dest, deadline=deadline)
        fetched = self.run(["rev-parse", "FETCH_HEAD^{commit}"], cwd=dest, deadline=deadline)
        if fetched.strip() != commit_sha:
            raise CloneError("commit_mismatch", "The server returned a different commit.")

        size, files, submodules = self._tree_size(commit_sha, dest, deadline=deadline)
        if size > self._settings.max_checkout_bytes:
            raise CloneError(
                "repo_too_large",
                f"The repository is {size / 1024**3:.1f} GB checked out; the limit is "
                f"{self._settings.max_checkout_bytes / 1024**3:.1f} GB.",
            )
        self.run(
            ["checkout", "--detach", "--force", "--quiet", commit_sha], cwd=dest, deadline=deadline
        )
        self.run(["clean", "-ffdxq"], cwd=dest, deadline=deadline)
        (dest / ".git" / LAST_USED_MARKER).write_text(str(time.time()))
        return Checkout(dest, commit_sha, size, files, submodules)

    def _fetch(self, source: CloneSource, commit_sha: str, dest: Path, *, deadline: float) -> None:
        args = ["fetch", "--depth=1", "--no-tags", "--no-recurse-submodules", "--quiet"]
        try:
            self.run([*args, "origin", commit_sha], cwd=dest, deadline=deadline, token=source.token)
        except GitCommandError as exc:
            text = exc.stderr.lower()
            if any(marker in text for marker in _TRANSIENT):
                raise TransientGitError(exc.stderr) from None
            if any(marker in text for marker in _AUTH):
                raise CloneError(
                    "clone_auth_failed", "GitHub refused access to the repository."
                ) from None
            if any(marker in text for marker in _NOT_FOUND) or "not our ref" in text:
                raise CloneError(
                    "repo_not_found", "The repository or commit wasn't found on GitHub."
                ) from None
            raise CloneError("clone_failed", "Git couldn't fetch the repository.") from None

    def _tree_size(self, commit_sha: str, dest: Path, *, deadline: float) -> tuple[int, int, int]:
        listing = self.run(["ls-tree", "-r", "-l", "-z", commit_sha], cwd=dest, deadline=deadline)
        size = files = submodules = 0
        for entry in filter(None, listing.split("\0")):
            meta, _, _path = entry.partition("\t")
            mode, kind, _object, length = meta.split(maxsplit=3)
            if mode == _SUBMODULE_MODE:
                submodules += 1
            elif kind == "blob":
                files += 1
                size += int(length) if length.strip().isdigit() else 0
        return size, files, submodules

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        allowed = {"https", "file"} if self._settings.allow_file_protocol else {"https"}
        if parts.scheme not in allowed or parts.username or parts.password:
            raise CloneError("invalid_remote", "Only HTTPS remotes without credentials are used.")

    # ------------------------------------------------------------------ running git

    def environment(self, token: str | None = None) -> dict[str, str]:
        """The whole environment git gets (nothing inherited but PATH)."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self._home),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(self._askpass),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        if token:
            env["SENSEI_GIT_TOKEN"] = token
        return env

    def _argv(self, args: list[str]) -> list[str]:
        protocols = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
        if self._settings.allow_file_protocol:
            protocols += ["-c", "protocol.file.allow=always"]
        return [
            self._settings.git_binary,
            "-c", "credential.helper=",  # no stored credentials, ours only
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "gc.auto=0",
            "-c", "maintenance.auto=false",
            "-c", "advice.detachedHead=false",
            *protocols,
            *args,
        ]  # fmt: skip

    def run(self, args: list[str], *, cwd: Path, deadline: float, token: str | None = None) -> str:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise CloneError("clone_timeout", "Cloning took too long.")
        process = subprocess.Popen(  # noqa: S603 - fixed program, arguments built here
            self._argv(args),
            cwd=cwd,
            env=self.environment(token),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # so a timeout can stop git's helpers too
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise CloneError("clone_timeout", "Cloning took too long.") from None
        if process.returncode != 0:
            raise GitCommandError(args[0], redact_text(stderr.strip())[:2000])
        return stdout

    def close(self) -> None:
        for path in sorted(self._private.rglob("*"), reverse=True):
            path.unlink() if not path.is_dir() else path.rmdir()
        self._private.rmdir()


class GitCommandError(Exception):
    def __init__(self, command: str, stderr: str) -> None:
        super().__init__(f"git {command} failed: {stderr}")
        self.command = command
        self.stderr = stderr
