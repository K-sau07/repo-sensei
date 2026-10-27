"""JavaScript and TypeScript imports as graph facts, for the graph stage: :func:`extract` in the
parse pool (on the tree), :func:`resolve` over the whole snapshot.

- :func:`extract` keeps what a file imports and exports (:class:`FileExports`): plain data, so
  no tree leaves the worker process.
- :func:`resolve` reads the project's configuration (tsconfig, package.json) from the graph
  stage's :class:`~sensei_engine.graph.project.Project`, resolves every file's imports
  (:mod:`sensei_engine.imports.ecma.table`) and returns ``IMPORTS`` edges from each file's
  module to what each statement names, with the stats and each module's import table (local
  name → the key it is bound to):

  - a definition of the repository: an edge to its symbol key; a module (``import * as``, a
    side-effect import, a name the module doesn't define): to its module key;
  - outside the repository: a named or default import goes to an ``external_symbol`` node
    (``external_symbol npm:react/useState``), anything else to the package's ``package`` node
    (``package npm:react``); Node's built-ins are ``npm:node:fs``;
  - a file that isn't code (``./App.css``, ``./logo.svg``, ``./en.json``) has no node: left out.

Edges are owned by the importing file, with the statement's line as evidence and
``import_resolved``'s confidence; a type-only import is marked (``attrs.type_only``). External
nodes are shared by every importer (no owner): a node owned by one importer would go, and take
the others' edges with it, whenever that file changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.project import Project
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.imports.ecma.config import ProjectConfig
from sensei_engine.imports.ecma.resolve import ModuleResolver, ResolutionKind
from sensei_engine.imports.ecma.statements import (
    DEFAULT,
    ImportKind,
    extract_exports,
    extract_imports,
)
from sensei_engine.imports.ecma.table import FileExports, ImportTables, TargetKind
from sensei_engine.ingest.languages import detect_language
from sensei_engine.parse.parser import ParseResult
from sensei_engine.symbols.extract import extract_symbols
from sensei_engine.symbols.keys import KEY_LANGUAGES, module_key

LANGUAGES = frozenset({"javascript", "jsx", "typescript", "tsx"})
_REEXPORTS = frozenset({ImportKind.REEXPORT, ImportKind.REEXPORT_ALL})
HEAD_BYTES = 256  # enough for a shebang (a script with no extension)


@dataclass(slots=True)
class ImportStats:
    """What the stage notes (the same fields as Python's), and the import tables."""

    internal: int = 0  # imports of the repository's own files
    resolved: int = 0  # ... resolved to a file
    external: int = 0  # of packages outside it
    unresolved: list[tuple[str, int, str]] = field(
        default_factory=list[tuple[str, int, str]]
    )  # (importer, line, specifier)
    # Module key → local name → the key of what it is bound to (for the module nodes' attrs).
    tables: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])

    @property
    def rate(self) -> float:
        return self.resolved / self.internal if self.internal else 1.0


def extract(parsed: ParseResult) -> FileExports | None:
    """What ``parsed`` imports and exports; None for a file that isn't JavaScript or
    TypeScript, or didn't parse. Exported names are read from its definitions, keyed as the
    symbols stage keys them."""
    language = detect_language(parsed.path, parsed.source[:HEAD_BYTES])
    if not parsed.ok or language not in LANGUAGES:
        return None
    return FileExports.of(
        parsed.path,
        extract_imports(parsed),
        extract_exports(parsed),
        extract_symbols(parsed, language),
    )


def resolve(
    project: Project, facts: Mapping[str, FileExports]
) -> tuple[list[GraphNode], list[GraphEdge], ImportStats]:
    """Every file's import edges, the external nodes they reach and the stats (see the
    module)."""
    configs = project.configs

    def read(path: str) -> bytes | None:
        text = configs.get(path)
        return text.encode() if text is not None else None

    config = ProjectConfig.load(project.paths, read)
    tables = ImportTables(ModuleResolver(project.paths, config), facts)
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    stats = ImportStats()
    for path in sorted(facts):
        edges.extend(import_edges(tables, path, nodes, stats))
    return list(nodes.values()), edges, stats


def external_package_key(package: str) -> str:
    return f"package npm:{package}"


def external_symbol_key(package: str, name: str) -> str:
    return f"external_symbol npm:{package}/{name}"


def _external_node(nodes: dict[str, GraphNode], package: str, name: str | None) -> str:
    """The key of the package's node, or of its name's; added to ``nodes`` if new."""
    if name is None:
        key, kind = external_package_key(package), NodeKind.PACKAGE
        label, qualified = package, package
    else:
        key, kind = external_symbol_key(package, name), NodeKind.EXTERNAL_SYMBOL
        label, qualified = (package if name == DEFAULT else name), f"{package}.{name}"
    if key not in nodes:
        nodes[key] = GraphNode(
            key=key,
            kind=kind,
            name=label,
            qualified_name=qualified,
            lang="javascript",
            attrs={"package": package},
        )
    return key


def import_edges(
    tables: ImportTables,
    path: str,
    nodes: dict[str, GraphNode],
    stats: ImportStats | None = None,
) -> list[GraphEdge]:
    """``path``'s ``IMPORTS`` edges; the external nodes they reach are added to ``nodes``, and
    the counts and the file's import table to ``stats``."""
    language = detect_language(path)
    if language not in KEY_LANGUAGES:
        return []
    source = module_key(language, path)
    stats = stats if stats is not None else ImportStats()
    table: dict[str, str] = {}
    edges: list[GraphEdge] = []
    for binding in tables.bindings(path):
        target, statement = binding.target, binding.statement
        kind = binding.resolution.kind
        if kind is ResolutionKind.EXTERNAL:
            stats.external += 1
        else:
            stats.internal += 1
            if kind is ResolutionKind.FILE:
                stats.resolved += 1
            else:
                stats.unresolved.append((path, statement.line, statement.source))
        if target.kind in (TargetKind.SYMBOL, TargetKind.MODULE) and target.symbol_key:
            dst = target.symbol_key
        elif target.kind is TargetKind.EXTERNAL and target.package:
            dst = _external_node(nodes, target.package, target.name)
        else:
            continue  # unresolved, or a file that isn't code
        if statement.local is not None and statement.kind not in _REEXPORTS:
            table[statement.local] = dst
        edges.append(
            GraphEdge(
                src=source,
                dst=dst,
                type=EdgeType.IMPORTS,
                provenance=Provenance.IMPORT_RESOLVED,
                evidence_path=path,
                evidence_line=statement.line,
                attrs={"type_only": True} if statement.type_only else {},
                owner=path,
            )
        )
    if table:
        stats.tables[source] = table
    return edges
