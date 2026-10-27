"""Python import resolution (3.2): which module or definition each import of a file names.

1. **Package roots** (:func:`package_roots`): where dotted module names start. A folder holding
   a ``pyproject.toml``, ``setup.cfg`` or ``setup.py`` (and its ``src/``), the folder above every
   top-level package (a folder with ``__init__.py`` whose parent has none), and the repository
   root, which also covers namespace packages (PEP 420: folders without ``__init__.py``).
2. **Module map** (:class:`ModuleMap`): dotted name to file, every file named from each root above
   it, the deepest root's name first (``backend/app/models.py`` is ``app.models`` under
   ``backend/``); ``__init__.py`` is its package.
3. **Resolution** of every form: ``import a.b``, ``import a.b as c``, ``from a.b import c as d``,
   relative imports (``from ..x import y``), ``from x import *`` (the module's ``__all__``, else
   its public names), and names a package re-exports from its ``__init__`` (followed up to
   :data:`MAX_HOPS` imports deep). A name ``from`` a module that isn't a definition there is a
   submodule (``from app.api import deps``) when one exists.

Each file gets an **import table** (local name to the key it means, :func:`import_tables`, kept in
the module node's ``attrs["imports"]`` for call resolution, 3.4) and ``IMPORTS`` edges from its
module to every module or definition it imports (``import_resolved``, 0.9). What no file of the
repository defines is an external: ``import x`` a ``package py:x`` node, ``from x import y`` an
``external_symbol py:x/y`` node (both shared, owned by no file).
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from posixpath import dirname, join
from typing import cast

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.project import Project
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.parse.languages import QueryKind
from sensei_engine.parse.parser import ParseResult

MAX_HOPS = 5  # re-exports followed through __init__ files
SOURCE_SUFFIXES = (".py", ".pyi")
PROJECT_FILES = ("pyproject.toml", "setup.cfg", "setup.py")


@dataclass(frozen=True, slots=True)
class PyImport:
    line: int  # 1-based
    kind: str  # "import" (import a.b), "from", "wildcard" (from a import *), "future"
    source: str  # "a.b", or relative with its dots: "..x", "."
    name: str | None = None  # from-imports: the imported name
    alias: str | None = None  # the "as" name

    @property
    def local(self) -> str | None:
        """The name it binds in the file (None: a wildcard, a __future__ import)."""
        if self.kind == "import":
            return self.alias or self.source.split(".")[0]
        if self.kind == "from":
            return self.alias or self.name
        return None


@dataclass(frozen=True, slots=True)
class PyFacts:
    """What a file's imports are (read from its tree in the parse pool)."""

    imports: tuple[PyImport, ...] = ()
    all_names: tuple[str, ...] | None = None  # its __all__, if it lists one


def _text(node: object) -> str:
    raw: bytes = getattr(node, "text", b"") or b""
    return raw.decode("utf-8", "replace")


def extract(parsed: ParseResult) -> PyFacts:
    """A file's imports and ``__all__``, from its parse (in the pool: no tree leaves it)."""
    found: list[PyImport] = []
    for match in parsed.captures.get(QueryKind.IMPORTS, ()):
        line = match.node.start_point[0] + 1
        source, name, alias = (match.role(role) for role in ("source", "name", "alias"))
        kind = match.kind.removeprefix("import").lstrip(".") or "import"
        if kind == "future":
            found.append(PyImport(line, "future", "__future__", _text(name)))
        elif source is not None:
            found.append(
                PyImport(
                    line,
                    kind,
                    _text(source),
                    _text(name) if name is not None and kind == "from" else None,
                    _text(alias) if alias is not None else None,
                )
            )
    return PyFacts(tuple(found), _all_names(parsed.source))


def _all_names(source: bytes) -> tuple[str, ...] | None:
    """A module's ``__all__`` when it is a literal list or tuple of strings."""
    if b"__all__" not in source:
        return None
    try:
        module = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    for statement in module.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        if (
            any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)
            and isinstance(value, ast.List | ast.Tuple)
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts)
        ):
            return tuple(str(e.value) for e in value.elts if isinstance(e, ast.Constant))
    return None


# ---------------------------------------------------------------------- the module map


def _is_identifier(part: str) -> bool:
    return part.isidentifier()


def package_roots(paths: Iterable[str], configs: Mapping[str, str] | None = None) -> list[str]:
    """Where module names start (see the module), deepest first; ``""`` is the repository."""
    listed = set(paths)
    roots: set[str] = {""}
    for path in listed:
        folder, name = dirname(path), path.rsplit("/", 1)[-1]
        if name in PROJECT_FILES:
            roots.add(folder)
            if any(p.startswith(join(folder, "src") + "/") for p in listed):
                roots.add(join(folder, "src"))
        if name == "__init__.py":
            parent = dirname(folder)
            if join(parent, "__init__.py") not in listed:
                roots.add(parent)
    for path, content in (configs or {}).items():
        if path.rsplit("/", 1)[-1] == "pyproject.toml":
            roots.update(_configured_roots(dirname(path), content))
    return sorted(roots, key=lambda root: (-root.count("/") - bool(root), root))


def _at(data: object, *keys: str) -> object:
    """``data[k1][k2]...``, or None where a level isn't a table."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = cast("dict[str, object]", data).get(key)
    return data


def _configured_roots(folder: str, content: str) -> set[str]:
    """``where`` / ``package-dir`` folders a pyproject names (setuptools, poetry)."""
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return set()
    found: set[str] = set()
    where = _at(data, "tool", "setuptools", "packages", "find", "where")
    if isinstance(where, list):
        found.update(w for w in cast("list[object]", where) if isinstance(w, str))
    package_dir = _at(data, "tool", "setuptools", "package-dir", "")
    if isinstance(package_dir, str):
        found.add(package_dir)
    packages = _at(data, "tool", "poetry", "packages")
    for package in cast("list[object]", packages) if isinstance(packages, list) else []:
        source = _at(package, "from")
        if isinstance(source, str):
            found.add(source)
    return {join(folder, w).rstrip("/").removeprefix("./") for w in found if w not in ("", ".")}


@dataclass(slots=True)
class ModuleMap:
    """Dotted module names and their files (see the module)."""

    by_name: dict[str, str] = field(default_factory=dict[str, str])
    names: dict[str, str] = field(default_factory=dict[str, str])  # path -> its first name

    @classmethod
    def build(cls, paths: Iterable[str], configs: Mapping[str, str] | None = None) -> ModuleMap:
        listed = sorted(set(paths))
        roots = package_roots(listed, configs)
        built = cls()
        for root in roots:  # deepest first: its names win
            prefix = f"{root}/" if root else ""
            for path in listed:
                if not path.endswith(SOURCE_SUFFIXES) or not path.startswith(prefix):
                    continue
                parts = path[len(prefix) :].rsplit(".", 1)[0].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if not parts or not all(_is_identifier(part) for part in parts):
                    continue
                dotted = ".".join(parts)
                # A .py beats a .pyi stub for the same name; a deeper root beats a shallower.
                if dotted not in built.by_name or built.by_name[dotted].endswith(".pyi"):
                    built.by_name[dotted] = path
                built.names.setdefault(path, dotted)
        return built

    def path(self, dotted: str) -> str | None:
        return self.by_name.get(dotted)

    def package_of(self, path: str) -> str | None:
        """The package a file's relative imports start from."""
        name = self.names.get(path)
        if name is None:
            return None
        if path.endswith(("/__init__.py", "/__init__.pyi")) or path in ("__init__.py",):
            return name
        return name.rsplit(".", 1)[0] if "." in name else ""


# ---------------------------------------------------------------------- resolution


@dataclass(frozen=True, slots=True)
class Target:
    key: str  # a node key: a module's or a definition's, or an external's
    path: str | None  # the file it is in (None: external)
    external: bool = False


@dataclass(slots=True)
class ImportStats:
    internal: int = 0  # imports of the repository's own modules
    resolved: int = 0  # ... resolved to a module or a definition (or what it re-exports)
    external: int = 0  # of packages outside it
    unresolved: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])

    @property
    def rate(self) -> float:
        return self.resolved / self.internal if self.internal else 1.0


class Resolver:
    """Resolves a project's Python imports (see the module)."""

    def __init__(self, project: Project, facts: Mapping[str, PyFacts]) -> None:
        self._project = project
        self._facts = facts
        self._modules = ModuleMap.build(project.paths, project.configs)
        self._tables: dict[str, dict[str, Target]] = {}
        self._resolving: set[str] = set()
        self.stats = ImportStats()

    def base(self, path: str, source: str) -> str | None:
        """The dotted module an import's source names, relative ones from the file's package."""
        if not source.startswith("."):
            return source
        package = self._modules.package_of(path)
        if package is None:
            return None
        dots = len(source) - len(source.lstrip("."))
        parts = package.split(".") if package else []
        if dots - 1 > len(parts):
            return None  # above the top package
        kept = parts[: len(parts) - (dots - 1)]
        rest = source[dots:]
        return ".".join([*kept, rest] if rest else kept)

    def module_name(self, path: str) -> str | None:
        """The file's dotted module name."""
        return self._modules.names.get(path)

    def module(self, dotted: str) -> Target | None:
        path = self._modules.path(dotted)
        if path is None:
            return None
        key = self._project.module_key(path)
        return Target(key, path) if key else None

    def name_in(self, dotted: str, name: str, hops: int = 0) -> Target | None:
        """What ``name`` means in module ``dotted``: a definition there, a submodule, or what
        the module imports under that name (a re-export, followed up to MAX_HOPS)."""
        path = self._modules.path(dotted)
        if path is not None:
            key = self._project.top_level(path).get(name)
            if key is not None:
                return Target(key, path)
        submodule = self.module(f"{dotted}.{name}")
        if submodule is not None:
            return submodule
        if path is not None and hops < MAX_HOPS:
            reexported = self.table(path, hops + 1).get(name)
            if reexported is not None:
                return reexported
        return None

    def table(self, path: str, hops: int = 0) -> dict[str, Target]:
        """The file's import table: each local name an import binds, to what it means."""
        if path in self._tables:
            return self._tables[path]
        if path in self._resolving:  # an import cycle: what's known so far
            return {}
        self._resolving.add(path)
        table: dict[str, Target] = {}
        for item in self._facts.get(path, PyFacts()).imports:
            for local, target in self._bind(path, item, hops):
                table[local] = target
        self._resolving.discard(path)
        self._tables[path] = table
        return table

    def _bind(self, path: str, item: PyImport, hops: int) -> list[tuple[str, Target]]:
        dotted = self.base(path, item.source)
        if dotted is None or item.kind == "future":
            return []
        if item.kind == "import":
            return [self._bind_import(item, dotted)]
        if item.kind == "wildcard":
            return self._bind_wildcard(dotted, hops)
        found = self._from(item, dotted, hops)
        return [(item.local, found)] if found is not None and item.local is not None else []

    def _bind_import(self, item: PyImport, dotted: str) -> tuple[str, Target]:
        if item.alias is not None:  # import a.b as c: c is a.b
            return item.alias, self.module(dotted) or _package(dotted)
        top = dotted.split(".", maxsplit=1)[0]  # import a.b: a is bound
        return top, self.module(top) or _package(top)

    def _bind_wildcard(self, dotted: str, hops: int) -> list[tuple[str, Target]]:
        module_path = self._modules.path(dotted)
        if module_path is None:
            return []
        names = self._facts.get(module_path, PyFacts()).all_names
        if names is None:
            public = set(self._project.top_level(module_path))
            public.update(self.table(module_path, hops + 1))
            names = tuple(sorted(n for n in public if not n.startswith("_")))
        bound = [(name, self.name_in(dotted, name, hops)) for name in names]
        return [(name, target) for name, target in bound if target is not None]

    def _from(self, item: PyImport, dotted: str, hops: int = 0) -> Target | None:
        """What ``from <dotted> import <name>`` imports: the repository's, else an external
        (never for a relative import, or a module the repository has)."""
        if item.name is None:
            return None
        found = self.name_in(dotted, item.name, hops)
        if found is None and not item.source.startswith(".") and not self._internal(dotted):
            found = _external(dotted, item.name)
        return found

    def edges(self, path: str) -> list[tuple[PyImport, Target]]:
        """Each of the file's imports and what it imports (counted in :attr:`stats`)."""
        found: list[tuple[PyImport, Target]] = []
        for item in self._facts.get(path, PyFacts()).imports:
            if item.kind == "future":
                continue
            dotted = self.base(path, item.source)
            internal = item.source.startswith(".") or (
                dotted is not None and self._internal(dotted)
            )
            target = self._target(item, dotted)
            if internal:
                self.stats.internal += 1
                # An external target here came through the module: a re-export (resolved).
                if target is not None:
                    self.stats.resolved += 1
                else:
                    self.stats.unresolved.append((path, item.line, item.source))
            elif target is not None:
                self.stats.external += 1
            if target is not None:
                found.append((item, target))
        return found

    def _internal(self, dotted: str) -> bool:
        """Whether the repository defines the module or a package above it."""
        parts = dotted.split(".")
        return any(self._modules.path(".".join(parts[:n])) for n in range(len(parts), 0, -1))

    def _target(self, item: PyImport, dotted: str | None) -> Target | None:
        if dotted is None:
            return None
        if item.kind == "from":
            return self._from(item, dotted)
        module = self.module(dotted)  # import a.b, from a.b import *
        if module is None and not item.source.startswith(".") and not self._internal(dotted):
            return _package(dotted)
        return module


def _package(dotted: str) -> Target:
    return Target(f"package py:{dotted.split('.', maxsplit=1)[0]}", None, external=True)


def _external(module: str, name: str) -> Target:
    return Target(f"external_symbol py:{module}/{name}", None, external=True)


def import_tables(project: Project, facts: Mapping[str, PyFacts]) -> dict[str, dict[str, str]]:
    """Each file's import table (local name to node key), by the file's module key: what
    ``GraphWriter.annotate({key: {"imports": table}})`` keeps on the module's node."""
    resolver = Resolver(project, facts)
    tables: dict[str, dict[str, str]] = {}
    for path in sorted(facts):
        module_key = project.module_key(path)
        if module_key is not None:
            tables[module_key] = {local: t.key for local, t in resolver.table(path).items()}
    return tables


def resolve(
    project: Project, facts: Mapping[str, PyFacts], resolver: Resolver | None = None
) -> tuple[list[GraphNode], list[GraphEdge], ImportStats]:
    """The project's Python import graph: external nodes, ``IMPORTS`` edges (each owned by the
    importing file), and how many imports resolved."""
    resolver = resolver or Resolver(project, facts)
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for path in sorted(facts):
        source_key = project.module_key(path)
        if source_key is None:
            continue
        for item, target in resolver.edges(path):
            if target.external:
                nodes.setdefault(target.key, external_node(target.key))
            edges.append(
                GraphEdge(
                    source_key,
                    target.key,
                    EdgeType.IMPORTS,
                    Provenance.IMPORT_RESOLVED,
                    evidence_path=path,
                    evidence_line=item.line,
                    owner=path,
                )
            )
    return list(nodes.values()), edges, resolver.stats


def external_node(key: str) -> GraphNode:
    """The node of an external: ``package py:x``, or ``external_symbol py:<module>/<name>``."""
    kind_name, _, rest = key.partition(" ")
    if kind_name == "package":
        package = rest.removeprefix("py:")
        return GraphNode(key, NodeKind.PACKAGE, package, lang="python", attrs={"package": package})
    module, _, name = rest.removeprefix("py:").partition("/")
    return GraphNode(
        key,
        NodeKind.EXTERNAL_SYMBOL,
        name,
        qualified_name=f"{module}.{name}",
        lang="python",
        attrs={"package": module.split(".")[0], "module": module},
    )
