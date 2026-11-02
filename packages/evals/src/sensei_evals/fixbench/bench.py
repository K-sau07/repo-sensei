"""A FixBench on disk::

    fixbench/
      fixbench.yaml          # version, the app's folder, each layer's test suite
      app/                   # the clean application (its tests pass)
      bugs/<id>/bug.yaml     # the issue, its risk, the hidden test
      bugs/<id>/bug.patch    # plants the bug (``git apply`` from the app's root)
      bugs/<id>/<test file>  # the hidden test

The gold fix is the bug's patch reversed. Nothing here runs anything.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

BENCH_FILE = "fixbench.yaml"
BUG_FILE = "bug.yaml"
PATCH_FILE = "bug.patch"
_ID_RE = re.compile(r"^[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")

Risk = Literal["low", "medium", "high"]


class BenchError(Exception):
    """The bench on disk isn't valid (what, and where)."""


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"not a relative path inside the app: {value!r}")
    return str(path)


class Command(BaseModel):
    """A test command: an argv run in ``workdir`` (a folder of the app)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workdir: str = ""
    command: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=300.0, gt=0)

    @field_validator("workdir")
    @classmethod
    def _workdir(cls, value: str) -> str:
        return _relative(value) if value else ""


class HiddenTest(Command):
    """The bug's hidden test: ``file`` (in the bug's folder) copied to ``path`` in the app."""

    file: str
    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _relative(value)


class BugSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    risk: Risk
    layer: str = Field(min_length=1)  # which of the bench's suites covers it
    issue: str = Field(min_length=1)  # what a user would file: the pipeline's only input
    stack_trace: str | None = None
    hidden_test: HiddenTest
    # Files the gold fix touches: for analysis, never given to a pipeline.
    fix_files: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(f"ids look like 07-off-by-one, got {value!r}")
        return value


class BenchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    app: str = "app"
    suites: dict[str, Command] = Field(min_length=1)  # by layer: backend, frontend, ...


class Bug(BaseModel):
    """A bug as loaded: its spec, its patch, its hidden test's source."""

    model_config = ConfigDict(frozen=True)

    spec: BugSpec
    patch: str
    hidden_source: str

    @property
    def id(self) -> str:
        return self.spec.id


class Bench(BaseModel):
    model_config = ConfigDict(frozen=True)

    root: Path
    spec: BenchSpec
    bugs: tuple[Bug, ...]

    @property
    def app_dir(self) -> Path:
        return self.root / self.spec.app

    def select(self, ids: list[str] | None) -> list[Bug]:
        """The bugs named (all if none), in the bench's order."""
        if not ids:
            return list(self.bugs)
        known = {bug.id: bug for bug in self.bugs}
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise BenchError(f"no such bugs: {', '.join(unknown)}")
        return [bug for bug in self.bugs if bug.id in set(ids)]


def _yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BenchError(f"{path} is missing") from None
    except yaml.YAMLError as error:
        raise BenchError(f"{path} isn't valid YAML: {error}") from None


def _model[T: BaseModel](model: type[T], data: object, where: Path) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise BenchError(f"{where}:\n{error}") from None


def load_bench(root: Path) -> Bench:
    spec = _model(BenchSpec, _yaml(root / BENCH_FILE), root / BENCH_FILE)
    if not (root / spec.app).is_dir():
        raise BenchError(f"the app folder {root / spec.app} is missing")
    bugs: list[Bug] = []
    folder = root / "bugs"
    for bug_dir in sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []:
        bug = _model(BugSpec, _yaml(bug_dir / BUG_FILE), bug_dir / BUG_FILE)
        if bug.id != bug_dir.name:
            raise BenchError(f"{bug_dir}: its id is {bug.id!r}, its folder {bug_dir.name!r}")
        if bug.layer not in spec.suites:
            raise BenchError(f"{bug_dir}: layer {bug.layer!r} has no suite in {BENCH_FILE}")
        try:
            patch = (bug_dir / PATCH_FILE).read_text(encoding="utf-8")
            hidden = (bug_dir / bug.hidden_test.file).read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise BenchError(f"{bug_dir}: {Path(str(error.filename)).name} is missing") from None
        bugs.append(Bug(spec=bug, patch=patch, hidden_source=hidden))
    if not bugs:
        raise BenchError(f"{folder} has no bugs")
    return Bench(root=root, spec=spec, bugs=tuple(bugs))
