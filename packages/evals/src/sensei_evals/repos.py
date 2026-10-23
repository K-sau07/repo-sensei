"""The pinned benchmark repositories (``evals/private/repos.yaml``) and their local checkouts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from sensei_evals import paths
from sensei_evals.schema import SHA


class PinnedRepo(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    url: str
    sha: str

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


def load_renamed(checkouts: Path) -> dict[str, PinnedRepo]:
    """Renamed copies made by ``sensei-eval rename``: each ``<id>.mapping.json`` beside a checkout
    pins that copy to the commit it was given."""
    found: dict[str, PinnedRepo] = {}
    for mapping in sorted(checkouts.glob("*.mapping.json")):
        sha = json.loads(mapping.read_text(encoding="utf-8")).get("sha")
        repo_id = mapping.name.removesuffix(".mapping.json")
        if isinstance(sha, str) and (checkouts / repo_id).is_dir():
            found[repo_id] = PinnedRepo(id=repo_id, url=(checkouts / repo_id).as_uri(), sha=sha)
    return found


def fetch_checkout(repo: PinnedRepo, directory: Path) -> Path:
    """A shallow checkout of exactly ``repo.sha`` in ``directory`` (made if missing)."""
    if paths.head_sha(directory) == repo.sha:
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(directory)]
    for args in (
        ["init", "-q"],
        ["fetch", "-q", "--depth", "1", repo.url, repo.sha],
        ["-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"],
    ):
        subprocess.run([*git, *args], check=True, timeout=1800)  # noqa: S603
    return directory
