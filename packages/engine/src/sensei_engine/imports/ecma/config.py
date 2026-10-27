"""The project configuration module resolution reads: ``tsconfig.json``/``jsconfig.json`` and
``package.json`` files, from a repository's files (nothing is installed, nothing runs).

**Compiler options** (:class:`TsConfig`): ``baseUrl`` and ``paths``, with ``extends`` followed
(a path or a list of them; a package to extend, such as ``@tsconfig/strictest``, isn't in the
repository and is skipped) and each option kept relative to the file that sets it, as
TypeScript does: ``paths`` targets resolve against ``baseUrl`` when one is set, else against the
directory of the config that sets ``paths``. ``include``/``files`` decide which files a config
covers, and ``references`` name the configs of a solution-style project (a root
``tsconfig.json`` that only points at ``tsconfig.app.json`` and ``tsconfig.node.json``).

A file's config (:meth:`ProjectConfig.config_for`) is the nearest ``tsconfig.json`` or
``jsconfig.json`` above it; if that one doesn't cover the file, the first of its references
that does.

**Packages** (:class:`Package`): every ``package.json``, its ``name``, entry points (``exports``,
``main``, ``module``) and declared dependencies. A package of the repository can be imported by
name from another (a monorepo) when it is a workspace (``workspaces`` in the root
``package.json``, or ``pnpm-workspace.yaml``) or when the importing package declares it.

Config files are JSON with comments and trailing commas (:func:`parse_jsonc`). One that doesn't
parse is skipped, as if it weren't there.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any, cast

from sensei_core.logging import get_logger

log = get_logger(__name__)

CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")
MAX_EXTENDS = 10  # configs an ``extends`` chain may pass through
MAX_CONFIG_BYTES = 1_000_000

Reader = Callable[[str], bytes | None]  # a repository file's bytes, or None if it isn't there


def _scan(text: str, keep: Callable[[str, int], int]) -> str:
    """``text`` with what ``keep`` drops outside strings: ``keep(text, i)`` gives how many
    characters at ``i`` to drop (0 keeps one)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == '"':
            end = i + 1
            while end < n and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[i : end + 1])
            i = end + 1
            continue
        drop = keep(text, i)
        if drop:
            i += drop
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _comment(text: str, i: int) -> int:
    if text.startswith("//", i):
        end = text.find("\n", i)
        return (len(text) if end < 0 else end) - i
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return (len(text) if end < 0 else end + 2) - i
    return 0


def _trailing_comma(text: str, i: int) -> int:
    if text[i] != ",":
        return 0
    rest = i + 1
    while rest < len(text) and text[rest] in " \t\r\n":
        rest += 1
    return 1 if rest < len(text) and text[rest] in "}]" else 0


def parse_jsonc(text: str) -> Any:
    """JSON with ``//`` and ``/* */`` comments and trailing commas (tsconfig's dialect).
    Raises ``ValueError`` if it still isn't JSON."""
    return json.loads(_scan(_scan(text, _comment), _trailing_comma))


def _read_json(read: Reader, path: str) -> dict[str, Any] | None:
    data = read(path)
    if data is None or len(data) > MAX_CONFIG_BYTES:
        return None
    try:
        value = parse_jsonc(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        log.info("config file skipped", path=path, error=type(exc).__name__)
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _join(directory: str, relative: str) -> str | None:
    """``relative`` from ``directory``, normalized; None if it leaves the repository."""
    joined = posixpath.normpath(posixpath.join(directory, relative)) if relative else directory
    if joined == ".":
        return ""
    if joined.startswith("../") or joined == ".." or joined.startswith("/"):
        return None
    return joined


@cache
def _glob(pattern: str) -> re.Pattern[str]:
    """A tsconfig/workspace glob as a regex: ``**/`` any directories, ``*`` and ``?`` within a
    segment."""
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts) + "$")


def _covers(pattern: str, path: str) -> bool:
    """Whether an ``include`` pattern (repo-relative) covers ``path``: a pattern with no
    wildcard and no extension is a directory and covers what is under it."""
    if not any(c in pattern for c in "*?"):
        last = pattern.rsplit("/", 1)[-1]
        if "." not in last:
            return pattern in ("", path) or path.startswith(pattern.rstrip("/") + "/")
        return path == pattern
    return _glob(pattern).match(path) is not None


@dataclass(frozen=True, slots=True)
class PathMapping:
    pattern: str  # as written: "@/*", "config"
    targets: tuple[str, ...]  # repo-relative, "*" kept: "frontend/src/*"

    def match(self, specifier: str) -> str | None:
        """What the ``*`` stands for in ``specifier`` ("" for an exact pattern), or None."""
        if "*" not in self.pattern:
            return "" if specifier == self.pattern else None
        prefix, _, suffix = self.pattern.partition("*")
        if (
            specifier.startswith(prefix)
            and specifier.endswith(suffix)
            and len(specifier) >= len(prefix) + len(suffix)
        ):
            return specifier[len(prefix) : len(specifier) - len(suffix)]
        return None


@dataclass(frozen=True, slots=True)
class TsConfig:
    path: str  # the config file, repo-relative
    base_url: str | None = None  # a directory, repo-relative
    paths: tuple[PathMapping, ...] = ()
    include: tuple[str, ...] | None = None  # repo-relative patterns
    files: tuple[str, ...] | None = None  # neither given: all under its directory
    references: tuple[str, ...] = ()  # config files, repo-relative

    @property
    def directory(self) -> str:
        return posixpath.dirname(self.path)

    def covers(self, path: str) -> bool:
        if self.files is not None and path in self.files:
            return True
        if self.include is None:
            # "files" alone lists everything (a solution's "files": [] covers nothing).
            return self.files is None and _covers(self.directory, path)
        return any(_covers(pattern, path) for pattern in self.include)

    def mappings(self, specifier: str) -> list[tuple[PathMapping, str]]:
        """The ``paths`` patterns ``specifier`` matches, the best first: TypeScript picks the
        pattern with the longest prefix before its ``*`` (an exact pattern beats them all)."""
        found = [(m, star) for m in self.paths if (star := m.match(specifier)) is not None]
        return sorted(
            found,
            key=lambda item: (
                -1 if "*" not in item[0].pattern else -len(item[0].pattern.partition("*")[0])
            ),
        )


@dataclass(slots=True)
class _Options:
    base_url: str | None = None
    paths_base: str = ""  # where paths resolve when there is no baseUrl
    include: tuple[str, ...] | None = None
    files: tuple[str, ...] | None = None
    raw_paths: Mapping[str, Any] | None = None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]  # pyright: ignore[reportUnknownVariableType]
    return []


def _config_path(directory: str, reference: str) -> str | None:
    """A config named by ``extends`` or ``references`` (a file, or a directory holding a
    ``tsconfig.json``); None for a package or a path out of the repository."""
    if not reference.startswith((".", "/")):
        return None  # a package: not in the repository
    path = _join(directory, reference.lstrip("/") if reference.startswith("/") else reference)
    if path is None:
        return None
    if path.endswith(".json"):
        return path
    return path + ".json"


def _load_options(read: Reader, path: str, seen: tuple[str, ...]) -> _Options | None:
    """A config's options with its ``extends`` applied (later bases override earlier ones,
    the config itself overrides them all)."""
    raw = _read_json(read, path)
    if raw is None:
        return None
    directory = posixpath.dirname(path)
    options = _Options()
    if len(seen) < MAX_EXTENDS:
        for base in _strings(raw.get("extends")):
            base_path = _config_path(directory, base)
            if base_path is None or base_path in seen:
                continue
            inherited = _load_options(read, base_path, (*seen, base_path))
            if inherited is None and not base_path.endswith("/tsconfig.json"):
                folder = base_path.removesuffix(".json")
                inherited = _load_options(read, f"{folder}/tsconfig.json", (*seen, base_path))
            if inherited is not None:
                _merge(options, inherited)
    compiler = raw.get("compilerOptions")
    if isinstance(compiler, dict):
        base_url = compiler.get("baseUrl")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(base_url, str):
            options.base_url = _join(directory, base_url)
        paths = compiler.get("paths")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if isinstance(paths, dict):
            options.raw_paths = paths  # pyright: ignore[reportUnknownMemberType]
            options.paths_base = directory
    if "include" in raw:
        options.include = tuple(
            p for item in _strings(raw["include"]) if (p := _join(directory, item)) is not None
        )
    if "files" in raw:
        options.files = tuple(
            p for item in _strings(raw["files"]) if (p := _join(directory, item)) is not None
        )
    return options


def _merge(into: _Options, base: _Options) -> None:
    for name in ("base_url", "include", "files", "raw_paths"):
        value = getattr(base, name)
        if value is not None:
            setattr(into, name, value)
    if base.raw_paths is not None:
        into.paths_base = base.paths_base


def _mappings(options: _Options) -> tuple[PathMapping, ...]:
    if not options.raw_paths:
        return ()
    base = options.base_url if options.base_url is not None else options.paths_base
    found: list[PathMapping] = []
    for pattern, targets in options.raw_paths.items():
        joined = tuple(t for item in _strings(targets) if (t := _join(base, item)) is not None)
        if joined:
            found.append(PathMapping(pattern, joined))
    return tuple(found)


def load_tsconfig(read: Reader, path: str) -> TsConfig | None:
    """The config at ``path`` (repo-relative), ``extends`` applied; None if it isn't there or
    doesn't parse."""
    options = _load_options(read, path, (path,))
    if options is None:
        return None
    raw = _read_json(read, path) or {}
    references: list[str] = []
    listed: Any = raw.get("references")
    for item in cast("list[Any]", listed) if isinstance(listed, list) else []:
        target = cast("dict[str, Any]", item).get("path") if isinstance(item, dict) else None
        if isinstance(target, str) and (ref := _config_path(posixpath.dirname(path), target)):
            references.append(ref)
    return TsConfig(
        path=path,
        base_url=options.base_url,
        paths=_mappings(options),
        include=options.include,
        files=options.files,
        references=tuple(references),
    )


@dataclass(frozen=True, slots=True)
class Package:
    directory: str  # repo-relative ("" for the root)
    name: str | None
    exports: Any = None  # package.json "exports", as written
    entries: tuple[str, ...] = ()  # "module", "main" (and "types"), repo-relative
    dependencies: frozenset[str] = frozenset()
    workspaces: tuple[str, ...] = ()  # repo-relative directory patterns


_DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def load_package(read: Reader, path: str) -> Package | None:
    raw = _read_json(read, path)
    if raw is None:
        return None
    directory = posixpath.dirname(path)
    name = raw.get("name")
    entries = tuple(
        entry
        for key in ("source", "module", "main", "types", "typings")
        if isinstance(value := raw.get(key), str) and (entry := _join(directory, value)) is not None
    )
    dependencies: set[str] = set()
    for key in _DEPENDENCY_FIELDS:
        declared = raw.get(key)
        if isinstance(declared, dict):
            dependencies.update(k for k in declared if isinstance(k, str))  # pyright: ignore[reportUnknownVariableType]
    workspaces = raw.get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return Package(
        directory=directory,
        name=name if isinstance(name, str) and name else None,
        exports=raw.get("exports"),
        entries=entries,
        dependencies=frozenset(dependencies),
        workspaces=tuple(
            w for item in _strings(workspaces) if (w := _join(directory, item)) is not None
        ),
    )


def _pnpm_workspaces(read: Reader) -> tuple[str, ...]:
    """The ``packages:`` globs of a root ``pnpm-workspace.yaml`` (a plain list; read by line,
    no YAML parser needed for its one shape)."""
    data = read("pnpm-workspace.yaml")
    if data is None:
        return ()
    found: list[str] = []
    inside = False
    for line in data.decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t", "-")):
            inside = stripped.startswith("packages:")
            continue
        if inside and stripped.startswith("-"):
            value = stripped[1:].strip().strip("'\"")
            if value and not value.startswith("!") and (path := _join("", value)) is not None:
                found.append(path)
    return tuple(found)


@dataclass(slots=True)
class ProjectConfig:
    """Every config and package of a repository (see the module)."""

    configs: dict[str, TsConfig] = field(default_factory=dict[str, TsConfig])
    packages: dict[str, Package] = field(default_factory=dict[str, Package])  # by directory
    workspaces: tuple[str, ...] = ()
    _config_dirs: dict[str, str] = field(default_factory=dict[str, str])  # dir -> config path

    @classmethod
    def load(cls, files: Collection[str], read: Reader) -> ProjectConfig:
        """The configs among ``files`` (repo-relative paths), read with ``read``."""
        project = cls()
        for path in sorted(files):
            name = posixpath.basename(path)
            directory = posixpath.dirname(path)
            if name in CONFIG_NAMES:
                config = load_tsconfig(read, path)
                if config is None:
                    continue
                project.configs[path] = config
                if name == "tsconfig.json" or directory not in project._config_dirs:
                    project._config_dirs[directory] = path
                for reference in config.references:
                    if reference not in project.configs and reference in files:
                        loaded = load_tsconfig(read, reference)
                        if loaded is not None:
                            project.configs[reference] = loaded
            elif name == "package.json":
                package = load_package(read, path)
                if package is not None:
                    project.packages[directory] = package
        root = project.packages.get("")
        project.workspaces = (root.workspaces if root else ()) + _pnpm_workspaces(read)
        return project

    def config_for(self, path: str) -> TsConfig | None:
        """The config that compiles ``path`` (see the module)."""
        directory: str = posixpath.dirname(path)
        while True:
            config_path = self._config_dirs.get(directory)
            if config_path is not None:
                return self._covering(self.configs[config_path], path)
            if not directory:
                return None
            directory = posixpath.dirname(directory)

    def _covering(self, config: TsConfig, path: str) -> TsConfig:
        if config.covers(path):
            return config
        for reference in config.references:
            referenced = self.configs.get(reference)
            if referenced is not None and referenced.covers(path):
                return referenced
        return config

    def package_for(self, path: str) -> Package | None:
        """The nearest ``package.json`` above ``path``."""
        directory: str = posixpath.dirname(path)
        while True:
            package = self.packages.get(directory)
            if package is not None:
                return package
            if not directory:
                return None
            directory = posixpath.dirname(directory)

    def is_workspace(self, package: Package) -> bool:
        return any(_covers(pattern, package.directory) for pattern in self.workspaces)

    def internal_packages(self) -> Iterable[Package]:
        return (p for p in self.packages.values() if p.name is not None)
