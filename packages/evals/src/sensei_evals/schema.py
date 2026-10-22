"""Golden suites: questions about a repository at a pinned commit, with the code that answers them.

A suite is a YAML file::

    name: fastapi-template
    version: 0
    items:
      - id: ft-flow-01
        repo: fastapi-template
        sha: b1125d8839482cb7bbae603ddc6d0081bf87532e
        query: where does the login form send the password, and what checks it?
        class: flow
        layers: [ui, client, handler, service]
        gold:
          - {layer: ui, path: frontend/src/routes/login.tsx, symbol: Login, lines: [43, 125]}
          - {layer: handler, path: backend/app/api/routes/login.py, symbol: login_access_token,
             lines: [22, 39]}

Paths are relative to the repository root. ``lines`` are 1-based and inclusive. A target with
a ``symbol`` must give the symbol's lines; a target without lines stands for the whole file.
:func:`load_suite` reports every problem with the line it is on.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Self, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SHA = re.compile(r"[0-9a-f]{40}")
ITEM_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")


class QueryClass(StrEnum):
    SYMBOL = "symbol"  # an identifier: where is X defined
    REGEX = "regex"  # a literal or pattern: route strings, env vars, TODOs
    CONCEPTUAL = "conceptual"  # behaviour in words; identifiers usually absent
    FLOW = "flow"  # UI -> API -> DB across layers
    ARCHITECTURE = "architecture"  # how a subsystem is structured
    IMPACT = "impact"  # what breaks if X changes
    UNANSWERABLE = "unanswerable"  # the code does not contain the answer


class Layer(StrEnum):
    UI = "ui"  # components, pages, routes on the client
    STATE = "state"  # hooks, stores, query caches
    CLIENT = "client"  # the API client the UI calls through
    ROUTE = "route"  # HTTP route registration
    HANDLER = "handler"  # the function serving a route
    SERVICE = "service"  # domain logic below the handler
    DB = "db"  # models, tables, migrations, queries
    WORKER = "worker"  # background jobs
    CONFIG = "config"  # settings, environment, build and deploy files
    TEST = "test"
    DOCS = "docs"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldTarget(_Strict):
    layer: Layer
    path: str
    symbol: str | None = None
    lines: tuple[int, int] | None = None

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or value != path.as_posix() or path.is_absolute() or ".." in path.parts:
            msg = "must be a normalized relative path (no leading '/', no '..', no './')"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _span(self) -> Self:
        if self.lines is not None:
            start, end = self.lines
            if start < 1 or end < start:
                msg = "lines must be [start, end] with 1 <= start <= end"
                raise ValueError(msg)
        if self.symbol is not None and self.lines is None:
            msg = "a target with a symbol must give the symbol's lines"
            raise ValueError(msg)
        return self


class GoldenItem(_Strict):
    model_config = ConfigDict(validate_by_name=True)

    id: str
    repo: str
    sha: str
    query: Annotated[str, Field(min_length=3)]
    class_: QueryClass = Field(validation_alias="class")  # `class` is a keyword
    layers: list[Layer] = Field(default_factory=list[Layer])
    gold: list[GoldTarget] = Field(default_factory=list[GoldTarget])
    answerable: bool = True
    notes: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ITEM_ID.fullmatch(value):
            msg = "must be lowercase letters, digits, '.', '_' or '-'"
            raise ValueError(msg)
        return value

    @field_validator("sha")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not SHA.fullmatch(value):
            msg = "must be a full 40-character lowercase commit sha"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.layers)) != len(self.layers):
            msg = "layers must not repeat"
            raise ValueError(msg)
        if self.class_ is QueryClass.UNANSWERABLE and self.answerable:
            msg = "an unanswerable item must set answerable: false"
            raise ValueError(msg)
        if not self.answerable:
            if self.gold:
                msg = "an unanswerable item has no gold targets"
                raise ValueError(msg)
            return self
        if not self.gold:
            msg = "an answerable item needs at least one gold target"
            raise ValueError(msg)
        gold_layers = {target.layer for target in self.gold}
        if missing := [layer.value for layer in self.layers if layer not in gold_layers]:
            msg = f"layers without a gold target: {', '.join(missing)}"
            raise ValueError(msg)
        if extra := sorted(layer.value for layer in gold_layers - set(self.layers)):
            msg = f"gold targets in layers the item doesn't list: {', '.join(extra)}"
            raise ValueError(msg)
        return self


class Suite(_Strict):
    name: Annotated[str, Field(min_length=1)]
    version: Annotated[int, Field(ge=0)]
    items: list[GoldenItem]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids: set[str] = set()
        queries: set[tuple[str, str]] = set()
        for item in self.items:
            if item.id in ids:
                msg = f"duplicate item id {item.id!r}"
                raise ValueError(msg)
            # One question, one answer: retrievers are asked by query text.
            if (item.repo, item.query) in queries:
                msg = f"item {item.id!r} repeats an earlier query for {item.repo}"
                raise ValueError(msg)
            ids.add(item.id)
            queries.add((item.repo, item.query))
        return self

    def for_repo(self, repo: str) -> list[GoldenItem]:
        return [item for item in self.items if item.repo == repo]

    @property
    def repos(self) -> list[str]:
        return sorted({item.repo for item in self.items})


# ---------------------------------------------------------------- loading


class SuiteProblem(BaseModel):
    line: int | None  # 1-based; None when the problem has no place in the file
    where: str  # dotted location, e.g. items.3.gold.0.lines
    message: str

    def render(self, source: str) -> str:
        place = f"{source}:{self.line}" if self.line is not None else source
        return (
            f"{place}: {self.where}: {self.message}" if self.where else f"{place}: {self.message}"
        )


class SuiteError(Exception):
    """A suite file that can't be loaded; ``problems`` say where and why."""

    def __init__(self, source: str, problems: list[SuiteProblem]) -> None:
        self.source = source
        self.problems = problems
        super().__init__("\n".join(problem.render(source) for problem in problems))


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML that rejects a key given twice in one mapping (PyYAML keeps the last)."""


def _no_duplicate_keys(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)  # pyright: ignore[reportUnknownMemberType]
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(  # pyright: ignore[reportUnknownMemberType]
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def _node_at(root: yaml.Node | None, loc: tuple[int | str, ...]) -> yaml.Node | None:
    """The deepest YAML node on the path ``loc`` (pydantic's error location)."""
    node = root
    for part in loc:
        if isinstance(node, yaml.MappingNode):
            child = next(
                (
                    (key, value)
                    for key, value in node.value
                    if isinstance(key, yaml.ScalarNode) and key.value == str(part)
                ),
                None,
            )
            if child is None:
                return node
            node = child[1]
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
            if part >= len(node.value):
                return node
            node = node.value[part]
        else:
            return node
    return node


def parse_suite(text: str, source: str = "<suite>") -> Suite:
    """Validate a suite from YAML text. Raises :class:`SuiteError`."""
    try:
        # The stubs leave compose's return type unknown; it is the document's root node.
        root = cast("yaml.Node | None", yaml.compose(text, Loader=yaml.SafeLoader))  # pyright: ignore[reportUnknownMemberType]
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        line = mark.line + 1 if mark is not None else None
        raise SuiteError(
            source, [SuiteProblem(line=line, where="", message=str(error.problem))]
        ) from None
    except yaml.YAMLError as error:
        raise SuiteError(source, [SuiteProblem(line=None, where="", message=str(error))]) from None
    try:
        return Suite.model_validate(data)
    except ValidationError as error:
        problems: list[SuiteProblem] = []
        for detail in error.errors(include_url=False):
            loc = tuple(detail["loc"])
            node = _node_at(root, loc)
            message = detail["msg"].removeprefix("Value error, ")
            problems.append(
                SuiteProblem(
                    line=node.start_mark.line + 1 if node is not None else None,
                    where=".".join(str(part) for part in loc),
                    message=message,
                )
            )
        raise SuiteError(source, problems) from None


def load_suite(path: Path) -> Suite:
    """Read and validate a suite file. Raises :class:`SuiteError`."""
    return parse_suite(path.read_text(encoding="utf-8"), source=str(path))
