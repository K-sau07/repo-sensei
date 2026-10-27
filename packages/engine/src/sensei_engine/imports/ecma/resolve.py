"""Which file a JavaScript or TypeScript import specifier names, the way TypeScript's
``bundler`` resolution (and Vite, webpack, Node) would, over the repository's files alone.

In order, for ``importer`` importing ``specifier``:

1. **Relative** (``./x``, ``../x``): from the importer's directory.
2. **Root-relative** (``/src/x``, as Vite allows): from the importer's package directory, then
   from its ``public/`` (Vite serves it at the root: ``/assets/logo.svg``).
3. **Node built-ins** (``node:fs``, ``fs``): external, as ``node:<name>``, unless the package
   declares a package of that name (``util``, ``events`` from npm).
4. **tsconfig ``paths``** (``@/components/x``): the best pattern's targets, in order.
5. **tsconfig ``baseUrl``** (``components/x`` with ``baseUrl: "src"``), unless the specifier
   names a dependency the importer's package declares: ``i18next`` is the npm package even next
   to an ``src/i18next.d.ts`` that only augments its types (TypeScript would pick the
   declaration file; for what runs, the package is right).
6. **The repository's own packages** (a monorepo's ``@acme/ui``): ``exports`` (conditions
   preferred: ``source``, ``development``, ``import``, ``module``, ``browser``, ``default``,
   ``require``, ``node``, ``types``), then ``source``/``module``/``main``, then ``index``. An
   entry built into ``dist/`` (not in the repository) is looked for in ``src/``.
7. Anything else is an **external** package, by name (``@tanstack/react-query``).

A Docusaurus site's aliases (``@site/…``, a swizzled ``@theme/…``) are read before ``paths``: its
tsconfig extends ``@tsconfig/docusaurus``, which isn't in the repository. A ``paths`` pattern of
the project's own that matches and names no file is unresolved (a broken import), not external.

A path names a file as written (``./App.css``, ``./x.ts``); else with an extension (``.ts``,
``.tsx``, ``.mts``, ``.cts``, ``.js``, ``.jsx``, ``.mjs``, ``.cjs``, then ``.d.ts``); else as a
directory's ``index``. ``./x.js`` names ``x.ts`` or ``x.tsx`` first (TypeScript's ESM style).
Vite's query suffixes (``./logo.svg?react``, ``?raw``) are dropped.
"""

from __future__ import annotations

import enum
import posixpath
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import Any

from sensei_engine.imports.ecma.config import Package, ProjectConfig, TsConfig

EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".d.ts")
_TS_FOR_JS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
CONDITIONS = (
    "source",
    "development",
    "import",
    "module",
    "browser",
    "default",
    "require",
    "node",
    "types",
)
DOCUSAURUS = "@docusaurus/core"
_BUILD_DIRS = ("dist", "build", "lib", "out", "esm", "cjs")
NODE_BUILTINS = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)


class Via(enum.StrEnum):
    RELATIVE = "relative"
    ROOT = "root"
    PATHS = "paths"
    BASE_URL = "base_url"
    PACKAGE = "package"  # one of the repository's own packages


class ResolutionKind(enum.StrEnum):
    FILE = "file"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"  # it should be in the repository, and isn't


@dataclass(frozen=True, slots=True)
class Resolution:
    kind: ResolutionKind
    path: str | None = None  # the file (FILE)
    package: str | None = None  # the package's name (EXTERNAL; FILE via a package)
    via: Via | None = None

    @property
    def resolved(self) -> bool:
        return self.kind is not ResolutionKind.UNRESOLVED


def package_name(specifier: str) -> tuple[str, str]:
    """``("@scope/name", "./sub")`` for ``@scope/name/sub``; the subpath is ``"."`` if none."""
    parts = specifier.split("/")
    count = 2 if specifier.startswith("@") and len(parts) > 1 else 1
    name = "/".join(parts[:count])
    rest = "/".join(parts[count:])
    return name, f"./{rest}" if rest else "."


def _strip_query(specifier: str) -> str:
    return specifier.split("?", 1)[0] if "?" in specifier else specifier


def _inside(directory: str, relative: str) -> str | None:
    joined = posixpath.normpath(posixpath.join(directory, relative)) if relative else directory
    if joined in (".", ""):
        return ""
    if joined == ".." or joined.startswith(("../", "/")):
        return None
    return joined


def _export_targets(value: Any) -> Iterator[str]:
    """The paths an ``exports`` value names, the preferred condition first."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:  # pyright: ignore[reportUnknownVariableType]
            yield from _export_targets(item)
    elif isinstance(value, dict):
        for condition in CONDITIONS:
            if condition in value:
                yield from _export_targets(value[condition])
        for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
            if key not in CONDITIONS:
                yield from _export_targets(item)


def _exports_for(exports: Any, subpath: str) -> Iterator[str]:
    """What ``exports`` maps ``subpath`` (``"."``, ``"./sub"``) to."""
    if not isinstance(exports, dict) or not any(
        isinstance(k, str) and k.startswith(".")
        for k in exports  # pyright: ignore[reportUnknownVariableType]
    ):
        if subpath == ".":
            yield from _export_targets(exports)
        return
    entries: dict[str, Any] = exports  # pyright: ignore[reportUnknownVariableType]
    if subpath in entries:
        yield from _export_targets(entries[subpath])
        return
    patterns = sorted((k for k in entries if "*" in k), key=lambda k: -len(k.partition("*")[0]))
    for pattern in patterns:
        prefix, _, suffix = pattern.partition("*")
        if subpath.startswith(prefix) and subpath.endswith(suffix):
            star = subpath[len(prefix) : len(subpath) - len(suffix)]
            for target in _export_targets(entries[pattern]):
                yield target.replace("*", star)
            return


def _builtin(specifier: str, name: str, declared: bool) -> Resolution | None:
    """Step 3: one of Node's built-in modules."""
    if specifier.startswith("node:"):
        return Resolution(ResolutionKind.EXTERNAL, package=name)
    if name in NODE_BUILTINS and not declared:
        return Resolution(ResolutionKind.EXTERNAL, package=f"node:{name}")
    return None


class ModuleResolver:
    """Resolves specifiers over a repository's ``files`` (repo-relative paths)."""

    def __init__(self, files: Collection[str], project: ProjectConfig) -> None:
        self._files = frozenset(files)
        self.project = project
        self._by_name: dict[str, list[Package]] = {}
        for package in project.internal_packages():
            if package.name is not None:
                self._by_name.setdefault(package.name, []).append(package)

    def resolve(self, importer: str, specifier: str) -> Resolution:
        specifier = _strip_query(specifier.strip())
        if not specifier:
            return Resolution(ResolutionKind.UNRESOLVED)
        package = self.project.package_for(importer)
        if specifier in (".", "..") or specifier.startswith(("./", "../")):
            return self._found(_inside(posixpath.dirname(importer), specifier), Via.RELATIVE)
        if specifier.startswith("/"):
            return self._rooted(package, specifier.lstrip("/"))
        name, subpath = package_name(specifier)
        declared = package is not None and name in package.dependencies
        found = (
            _builtin(specifier, name, declared)
            or self._configured(importer, package, specifier, declared)
            or self._from_package(name, subpath, package)
        )
        return found or Resolution(ResolutionKind.EXTERNAL, package=name)

    # ------------------------------------------------------------------ steps

    def _rooted(self, package: Package | None, relative: str) -> Resolution:
        root = package.directory if package is not None else ""
        found = self._found(_inside(root, relative), Via.ROOT)
        return (
            found if found.resolved else self._found(_inside(root, f"public/{relative}"), Via.ROOT)
        )

    def _configured(
        self, importer: str, package: Package | None, specifier: str, declared: bool
    ) -> Resolution | None:
        """Steps 4 and 5 (and Docusaurus's aliases): what the project's configuration maps."""
        if package is not None and DOCUSAURUS in package.dependencies:
            found = self._docusaurus(package, importer, specifier)
            if found is not None:
                return found
        config = self.project.config_for(importer)
        if config is None:
            return None
        found = self._mapped(config, specifier)
        if found is not None or config.base_url is None or declared:
            return found
        path = self._file(_inside(config.base_url, specifier))
        return Resolution(ResolutionKind.FILE, path=path, via=Via.BASE_URL) if path else None

    def _from_package(self, name: str, subpath: str, importer: Package | None) -> Resolution | None:
        """Step 6: a file of the repository's package ``name``."""
        internal = self._internal(name, importer)
        if internal is None:
            return None
        path = self._package_file(internal, subpath)
        if path is None:
            return Resolution(ResolutionKind.UNRESOLVED, package=name, via=Via.PACKAGE)
        return Resolution(ResolutionKind.FILE, path=path, package=name, via=Via.PACKAGE)

    def _found(self, base: str | None, via: Via) -> Resolution:
        path = self._file(base)
        if path is None:
            return Resolution(ResolutionKind.UNRESOLVED, via=via)
        return Resolution(ResolutionKind.FILE, path=path, via=via)

    def _mapped(self, config: TsConfig, specifier: str) -> Resolution | None:
        """A file from ``paths``; unresolved when a pattern of the project's own (not a
        catch-all ``*``) matched and named no file: ``@/components/gone`` is a broken import,
        not a package."""
        matched = False
        for mapping, star in config.mappings(specifier):
            matched = matched or mapping.pattern != "*"
            for target in mapping.targets:
                path = self._file(target.replace("*", star, 1))
                if path is not None:
                    return Resolution(ResolutionKind.FILE, path=path, via=Via.PATHS)
        return Resolution(ResolutionKind.UNRESOLVED, via=Via.PATHS) if matched else None

    def _docusaurus(self, site: Package, importer: str, specifier: str) -> Resolution | None:
        """Docusaurus's aliases: ``@site/x`` is the site's own ``x``; ``@theme/X`` is the
        site's swizzled ``src/theme/X`` if it has one, else (and in that file itself, which
        imports the original's types) the theme package's."""
        if specifier.startswith("@site/"):
            return self._found(_inside(site.directory, specifier.removeprefix("@site/")), Via.PATHS)
        if specifier.startswith("@theme/"):
            name = specifier.removeprefix("@theme/")
            path = self._file(_inside(site.directory, f"src/theme/{name}"))
            if path is not None and path != importer:
                return Resolution(ResolutionKind.FILE, path=path, via=Via.PATHS)
        return None

    def _internal(self, name: str, importer: Package | None) -> Package | None:
        """The repository's package ``name``, if the importer may import it: a workspace, or
        a package the importer declares."""
        for package in self._by_name.get(name, ()):
            if package is importer:
                continue
            declared = importer is not None and name in importer.dependencies
            if declared or self.project.is_workspace(package):
                return package
        return None

    def _package_file(self, package: Package, subpath: str) -> str | None:
        root = package.directory
        for target in _exports_for(package.exports, subpath):
            path = self._file(_inside(root, target)) or self._source(root, target)
            if path is not None:
                return path
        if subpath == ".":
            for entry in package.entries:
                relative = posixpath.relpath(entry, root) if root else entry
                path = self._file(entry) or self._source(root, relative)
                if path is not None:
                    return path
            return self._file(_inside(root, "src/index")) or self._file(_inside(root, "index"))
        rest = subpath[2:]
        return self._file(_inside(root, rest)) or self._file(_inside(root, f"src/{rest}"))

    def _source(self, root: str, built: str) -> str | None:
        """A built entry's source: ``dist/index.js`` → ``src/index.ts``."""
        relative = posixpath.normpath(built)
        first, _, rest = relative.partition("/")
        if first not in _BUILD_DIRS or not rest:
            return None
        stem = rest
        for extension in (".d.ts", *EXTENSIONS):
            if stem.endswith(extension):
                stem = stem[: -len(extension)]
                break
        return self._file(_inside(root, f"src/{stem}"))

    def _file(self, base: str | None) -> str | None:
        """The file ``base`` names (see the module), or None."""
        if base is None:
            return None
        files = self._files
        stem, extension = posixpath.splitext(base)
        for swapped in _TS_FOR_JS.get(extension, ()):
            if stem + swapped in files:
                return stem + swapped
        if base in files:
            return base
        for extension in EXTENSIONS:
            if base + extension in files:
                return base + extension
        prefix = f"{base}/index" if base else "index"
        for extension in EXTENSIONS:
            if prefix + extension in files:
                return prefix + extension
        return None
