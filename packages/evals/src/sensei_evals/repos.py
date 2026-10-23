"""The pinned benchmark repositories (``evals/private/repos.yaml``) and their local checkouts."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from sensei_evals import paths
from sensei_evals.schema import SHA

REPO_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
URL_SCHEMES = frozenset({"https", "ssh", "file"})
FETCH_TIMEOUT = 1800


def valid_repo_id(value: str) -> bool:
    """A plain slug: it names a directory under the checkouts folder, so no ``/`` or ``..``."""
    return bool(REPO_ID.fullmatch(value)) and ".." not in value


class PinnedRepo(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    url: str
    sha: str

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not valid_repo_id(value):
            msg = "must be lowercase letters, digits, '.', '_' or '-' (no '..')"
            raise ValueError(msg)
        return value

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if urlparse(value).scheme not in URL_SCHEMES:
            msg = f"must be an {', '.join(sorted(URL_SCHEMES))} URL"
            raise ValueError(msg)
        return value

    @field_validator("sha")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not SHA.fullmatch(value):
            msg = "must be a full 40-character lowercase commit sha"
            raise ValueError(msg)
        return value


class _ReposFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repos: list[PinnedRepo]


def load_pinned(path: Path) -> dict[str, PinnedRepo]:
    """Pinned repositories by id; empty if the file doesn't exist (CI has no private files)."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {repo.id: repo for repo in _ReposFile.model_validate(data).repos}


MAPPING_SUFFIX = ".mapping.json"


def mapping_path(checkouts: Path, repo_id: str) -> Path:
    """Where ``sensei-eval rename`` writes the mapping of the copy ``checkouts/<repo_id>``:
    beside it, so the copy itself never holds (or indexes) its own key."""
    return checkouts / f"{repo_id}{MAPPING_SUFFIX}"


def read_mapping(path: Path) -> dict[str, Any] | None:
    """A mapping file's JSON object, or None if it isn't one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def load_renamed(checkouts: Path) -> dict[str, PinnedRepo]:
    """Renamed copies made by ``sensei-eval rename`` in ``checkouts`` (the default checkouts
    folder, or wherever ``--out`` put them): each ``<id>.mapping.json`` beside a copy ``<id>``
    pins that copy to the commit it was given. The id is the one stored in the mapping (older
    mappings without one go by their file name); a mapping whose id disagrees with its file
    name, or whose copy is missing, pins nothing."""
    found: dict[str, PinnedRepo] = {}
    for path in sorted(checkouts.glob(f"*{MAPPING_SUFFIX}")):
        mapping = read_mapping(path)
        if mapping is None:
            continue
        stem = path.name.removesuffix(MAPPING_SUFFIX)
        repo_id = mapping.get("repo", stem)
        sha = mapping.get("sha")
        if repo_id != stem or not valid_repo_id(stem) or not isinstance(sha, str):
            continue
        if SHA.fullmatch(sha) and (checkouts / stem).is_dir():
            found[stem] = PinnedRepo(id=stem, url=(checkouts / stem).as_uri(), sha=sha)
    return found


def _is_own_repository(directory: Path) -> bool:
    top = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return top.returncode == 0 and Path(top.stdout.strip()).resolve() == directory.resolve()


def fetch_checkout(repo: PinnedRepo, directory: Path) -> Path:
    """A clean, shallow checkout of exactly ``repo.sha`` in ``directory`` (made if missing).
    Local edits and untracked files in ``directory`` are discarded: it is a cache. Raises
    ``subprocess.CalledProcessError`` or ``subprocess.TimeoutExpired`` when git fails."""
    if paths.head_sha(directory) == repo.sha and not paths.has_uncommitted_changes(directory):
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(directory)]

    def run(*args: str) -> None:
        subprocess.run([*git, *args], check=True, timeout=FETCH_TIMEOUT)  # noqa: S603

    if not _is_own_repository(directory):
        run("init", "-q")
    if paths.head_sha(directory) != repo.sha:
        run("fetch", "-q", "--depth", "1", "--", repo.url, repo.sha)
        run("-c", "advice.detachedHead=false", "checkout", "-q", "-f", "FETCH_HEAD")
    else:
        run("-c", "advice.detachedHead=false", "checkout", "-q", "-f", repo.sha)
    if not _is_own_repository(directory):  # pragma: no cover - init above made it one
        msg = f"{directory} is not the root of its own git repository; not cleaning it"
        raise RuntimeError(msg)
    run("clean", "-q", "-f", "-d", "-x")
    return directory
