"""Per-file import tables: every name a JavaScript or TypeScript file imports, bound to the
symbol that defines it, through barrels.

A file's import (``import { Button } from "@/components/ui"``) names a module and a name in it.
The module is resolved to a file (:mod:`sensei_engine.imports.ecma.resolve`); the name is then
looked for among what that file exports:

1. a definition of its own, exported by that name (``export function Button``,
   ``export { Button as Btn }``, ``export default Button``);
2. a name it imports and exports again (``import { Button } from "./button"; export { Button }``);
3. a named re-export (``export { Button } from "./button"``, ``export * as ui from "./ui"``);
4. ``export * from "./button"``, for any name but ``default``, tried in order;
5. for a JavaScript module, its declaration file beside it (``policy.mjs``'s types in
   ``policy.d.mts``).

A barrel's chain is followed for up to :data:`MAX_HOPS` files (cycles stop it). A name that
isn't found ends at the module it was asked of: the module is right, the symbol unknown (a name
assigned to ``module.exports``, or a definition the symbol extractor doesn't see).

The graph's edges are made of these (:mod:`sensei_engine.imports.ecma.graph`).
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from sensei_engine.imports.ecma.resolve import ModuleResolver, Resolution, ResolutionKind
from sensei_engine.imports.ecma.statements import (
    DEFAULT,
    NAMESPACE,
    ImportKind,
    ImportStatement,
    LocalExport,
)
from sensei_engine.ingest.languages import detect_language
from sensei_engine.symbols.extract import Symbol, SymbolKind
from sensei_engine.symbols.keys import KEY_LANGUAGES, module_key

MAX_HOPS = 8  # files a barrel chain may pass through
# The declaration file beside a JavaScript module, for the types it declares.
_SIDECARS = {"js": ".d.ts", "mjs": ".d.mts", "cjs": ".d.cts", "jsx": ".d.ts"}


_REEXPORTS = frozenset({ImportKind.REEXPORT, ImportKind.REEXPORT_ALL})


class TargetKind(enum.StrEnum):
    SYMBOL = "symbol"  # a definition in the repository
    MODULE = "module"  # a code file as a whole
    FILE = "file"  # a file that isn't code (a stylesheet, an image, JSON)
    EXTERNAL = "external"  # a package from outside the repository
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class ImportTarget:
    kind: TargetKind
    path: str | None = None  # the file defining it (SYMBOL, MODULE, FILE)
    symbol_key: str | None = None  # SYMBOL
    package: str | None = None  # EXTERNAL
    name: str | None = None  # EXTERNAL: the name taken from the package (None: all of it)
    hops: int = 0  # barrel files passed through


@dataclass(frozen=True, slots=True)
class FileExports:
    """What one parsed file offers to its importers."""

    path: str
    statements: tuple[ImportStatement, ...] = ()
    local_exports: tuple[LocalExport, ...] = ()
    exported: Mapping[str, str] = field(default_factory=dict[str, str])  # name → symbol key
    defined: Mapping[str, str] = field(default_factory=dict[str, str])  # top-level name → key

    @classmethod
    def of(
        cls,
        path: str,
        statements: Sequence[ImportStatement],
        local_exports: Sequence[LocalExport],
        symbols: Iterable[Symbol],
    ) -> FileExports:
        exported: dict[str, str] = {}
        defined: dict[str, str] = {}
        symbols = list(symbols)
        module = next((s.symbol_key for s in symbols if s.kind is SymbolKind.MODULE), None)
        for symbol in symbols:
            if symbol.kind is SymbolKind.MODULE or symbol.container_key not in (None, module):
                continue
            defined.setdefault(symbol.name, symbol.symbol_key)
            attrs = symbol.attrs
            if attrs.get("default_export"):
                exported.setdefault(DEFAULT, symbol.symbol_key)
            if attrs.get("exported"):
                alias = attrs.get("export_alias")
                exported.setdefault(
                    alias if isinstance(alias, str) else symbol.name, symbol.symbol_key
                )
        return cls(path, tuple(statements), tuple(local_exports), exported, defined)


@dataclass(frozen=True, slots=True)
class ImportBinding:
    statement: ImportStatement
    resolution: Resolution
    target: ImportTarget


def module_target(path: str) -> ImportTarget:
    language = detect_language(path)
    if language in KEY_LANGUAGES:
        return ImportTarget(TargetKind.MODULE, path=path, symbol_key=module_key(language, path))
    return ImportTarget(TargetKind.FILE, path=path)


class ImportTables:
    """Import tables over a repository's parsed files (``files``: by path)."""

    def __init__(self, resolver: ModuleResolver, files: Mapping[str, FileExports]) -> None:
        self.resolver = resolver
        self.files = files
        self._resolved: dict[tuple[str, str], Resolution] = {}

    def resolve(self, importer: str, specifier: str) -> Resolution:
        key = (importer, specifier)
        found = self._resolved.get(key)
        if found is None:
            found = self._resolved[key] = self.resolver.resolve(importer, specifier)
        return found

    def bindings(self, path: str) -> list[ImportBinding]:
        """Every statement of ``path`` with what it names, in source order."""
        facts = self.files.get(path)
        if facts is None:
            return []
        return [self._bind(path, statement) for statement in facts.statements]

    def table(self, path: str) -> dict[str, ImportBinding]:
        """Local name → what it is bound to (re-exports bind nothing locally)."""
        return {
            binding.statement.local: binding
            for binding in self.bindings(path)
            if binding.statement.local is not None and binding.statement.kind not in _REEXPORTS
        }

    def _bind(self, path: str, statement: ImportStatement) -> ImportBinding:
        resolution = self.resolve(path, statement.source)
        if resolution.kind is ResolutionKind.EXTERNAL:
            target = _external(resolution, statement)
        elif resolution.kind is ResolutionKind.UNRESOLVED or resolution.path is None:
            target = ImportTarget(TargetKind.UNRESOLVED)
        elif statement.imported is None or statement.imported == NAMESPACE:
            target = module_target(resolution.path)
        else:
            found = self._exported(resolution.path, statement.imported, 0, frozenset())
            target = found if found is not None else module_target(resolution.path)
        return ImportBinding(statement, resolution, target)

    def _exported(
        self, path: str, name: str, hops: int, seen: frozenset[str]
    ) -> ImportTarget | None:
        """:meth:`export_of`, then the file's declaration sidecar: ``policy.mjs`` with its
        types in ``policy.d.mts``."""
        found = self.export_of(path, name, hops, seen)
        if found is not None:
            return found
        stem, dot, extension = path.rpartition(".")
        sidecar = _SIDECARS.get(extension) if dot else None
        if sidecar is None or f"{stem}{sidecar}" not in self.files:
            return None
        return self.export_of(f"{stem}{sidecar}", name, hops, seen)

    def export_of(
        self, path: str, name: str, hops: int = 0, seen: frozenset[str] = frozenset()
    ) -> ImportTarget | None:
        """The definition ``path`` exports as ``name`` (see the module), or None."""
        facts = self.files.get(path)
        if facts is None or path in seen or hops > MAX_HOPS:
            return None
        seen = seen | {path}
        found = self._named(facts, name, hops, seen)
        if found is not None or name == DEFAULT:
            return found
        return self._starred(facts, name, hops, seen)

    def _named(
        self, facts: FileExports, name: str, hops: int, seen: frozenset[str]
    ) -> ImportTarget | None:
        """Steps 1 to 3: exported by name, from here or through a named re-export."""
        key = facts.exported.get(name)
        if key is not None:
            return ImportTarget(TargetKind.SYMBOL, path=facts.path, symbol_key=key, hops=hops)
        for export in facts.local_exports:
            if export.exported == name:
                found = self._exported_local(facts, export.local, hops, seen)
                if found is not None:
                    return found
        for statement in facts.statements:
            if statement.kind is ImportKind.REEXPORT and statement.local == name:
                return self._through(facts.path, statement, hops, seen)
        return None

    def _exported_local(
        self, facts: FileExports, local: str, hops: int, seen: frozenset[str]
    ) -> ImportTarget | None:
        """What ``export { local }`` exports: a name the file imports, or one it defines."""
        for statement in facts.statements:
            if statement.local == local and statement.kind not in _REEXPORTS:
                return self._through(facts.path, statement, hops, seen)
        key = facts.defined.get(local)
        if key is None:
            return None
        return ImportTarget(TargetKind.SYMBOL, path=facts.path, symbol_key=key, hops=hops)

    def _starred(
        self, facts: FileExports, name: str, hops: int, seen: frozenset[str]
    ) -> ImportTarget | None:
        """Step 4: ``export * from``, in order."""
        for statement in facts.statements:
            if statement.kind is not ImportKind.REEXPORT_ALL:
                continue
            resolution = self.resolve(facts.path, statement.source)
            if resolution.kind is ResolutionKind.FILE and resolution.path is not None:
                found = self.export_of(resolution.path, name, hops + 1, seen)
                if found is not None:
                    return found
        return None

    def _through(
        self, path: str, statement: ImportStatement, hops: int, seen: frozenset[str]
    ) -> ImportTarget:
        """What a barrel's statement names, one file further."""
        resolution = self.resolve(path, statement.source)
        if resolution.kind is ResolutionKind.EXTERNAL:
            return replace(_external(resolution, statement), hops=hops + 1)
        if resolution.kind is not ResolutionKind.FILE or resolution.path is None:
            return ImportTarget(TargetKind.UNRESOLVED, hops=hops + 1)
        imported = statement.imported
        if imported is None or imported == NAMESPACE:
            return replace(module_target(resolution.path), hops=hops + 1)
        found = self._exported(resolution.path, imported, hops + 1, seen)
        return (
            found if found is not None else replace(module_target(resolution.path), hops=hops + 1)
        )


def _external(resolution: Resolution, statement: ImportStatement) -> ImportTarget:
    name = statement.imported if statement.imported != NAMESPACE else None
    return ImportTarget(TargetKind.EXTERNAL, package=resolution.package, name=name)
