"""What a language's graph resolver knows of the snapshot (Phase 3): its files, the config files
that shape resolution (``pyproject.toml``, ``tsconfig.json``, ``package.json``,
``pnpm-workspace.yaml``, Spring Boot's ``application.properties``, ...), and the
definitions the symbols stage extracted, by file.

Each language resolves its files' facts (imports, calls, ...) against a :class:`Project` into
graph nodes and edges (:mod:`sensei_engine.graph.writer`); the graph stage builds the project
once and runs every language's resolver over it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from posixpath import basename

# The files whose content resolution reads (by name).
CONFIG_PATTERNS = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "package.json",
    "tsconfig*.json",
    "jsconfig*.json",
    "pnpm-workspace.yaml",  # a pnpm monorepo's workspaces (3.3)
    "application*.properties",  # Spring Boot's: the servlet's context path (3.19)
    "application*.yml",
    "application*.yaml",
)


def is_config(path: str) -> bool:
    name = basename(path)
    return any(fnmatch(name, pattern) for pattern in CONFIG_PATTERNS)


@dataclass(frozen=True, slots=True)
class Definition:
    """One of a file's definitions, as the symbols stage saved it."""

    symbol_key: str
    name: str
    qualified_name: str  # "Item.save" for a method; the name at the top of the file
    kind: str  # a SYMBOL_KINDS value
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class Project:
    paths: frozenset[str]  # every file of the snapshot
    configs: Mapping[str, str] = field(default_factory=dict[str, str])  # content by path
    definitions: Mapping[str, Sequence[Definition]] = field(
        default_factory=dict[str, Sequence[Definition]]
    )

    def module_key(self, path: str) -> str | None:
        """The key of the file's module definition (what its imports go from)."""
        return next(
            (d.symbol_key for d in self.definitions.get(path, ()) if d.kind == "module"), None
        )

    def top_level(self, path: str) -> dict[str, str]:
        """The file's top-level definitions: name to key."""
        return {
            d.name: d.symbol_key
            for d in self.definitions.get(path, ())
            if d.kind != "module" and d.qualified_name == d.name
        }
