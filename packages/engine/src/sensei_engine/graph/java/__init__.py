"""Java's graph facts (3.18): imports and type names, bases, calls and Spring's dependency
injection, across files.

The graph stage calls :func:`extract` on each Java file in the parse pool (:mod:`.facts`: no tree
leaves it) and :func:`resolve` on them all: one :class:`~.imports.TypeIndex` of the repository's
types, by the package every file declares, serves the imports' and bases' edges
(:mod:`.imports`), the calls' (:mod:`.calls`) and the injections' (:mod:`.di`). Each file's
import table goes on its module's node (``attrs["imports"]``) and what the next stages read of
its types, methods and fields on theirs (``attrs["java"]``, :mod:`.attrs`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sensei_engine.graph.java import attrs, calls, di, imports
from sensei_engine.graph.java.facts import JavaFacts
from sensei_engine.graph.java.facts import extract as extract_facts
from sensei_engine.graph.project import Project
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.parse.parser import ParseResult

LANGUAGES = frozenset({"java"})


@dataclass(slots=True)
class JavaStats:
    imports: imports.ImportStats
    calls: calls.CallStats
    di: di.DiStats
    code: imports.CodeStats
    tables: dict[str, dict[str, str]]  # module key: its import table (what annotate keeps)
    attrs: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    # The stage reads these four (its imports'), the rest goes in its details as they are.
    @property
    def internal(self) -> int:
        return self.imports.internal

    @property
    def resolved(self) -> int:
        return self.imports.resolved

    @property
    def external(self) -> int:
        return self.imports.external

    @property
    def unresolved(self) -> list[tuple[str, int, str]]:
        return self.imports.unresolved


def extract(parsed: ParseResult) -> JavaFacts | None:
    """A Java file's package, imports, types, members, locals and calls (None: another
    language, or no parse)."""
    if parsed.language not in LANGUAGES or not parsed.ok:
        return None
    return extract_facts(parsed)


def resolve(
    project: Project, facts: Mapping[str, JavaFacts]
) -> tuple[list[GraphNode], list[GraphEdge], JavaStats]:
    """The project's Java graph: imports, bases, calls and injections, and their externals."""
    index = imports.TypeIndex(facts)
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    import_stats = imports.ImportStats()
    code_stats = imports.CodeStats()
    tables: dict[str, dict[str, str]] = {}
    for path in sorted(facts):
        found, table = imports.import_edges(index, path, import_stats, nodes)
        edges.extend(found)
        edges.extend(imports.base_edges(index, path, code_stats, nodes))
        if table:
            tables[facts[path].module] = table
    call_edges, call_stats, _ = calls.resolve_calls(index, facts, nodes)
    edges.extend(call_edges)
    di_stats = di.DiStats()
    kept: dict[str, dict[str, Any]] = {}
    beans = di.bean_classes(index)
    for path in sorted(facts):
        found, injects = di.resolve(index, path, di_stats, nodes, beans)
        edges.extend(found)
        kept.update(attrs.node_attrs(index, path, injects))
    code_stats.types = sum(len(f.types) for f in facts.values())
    code_stats.annotated = len(kept)
    return (
        list(nodes.values()),
        edges,
        JavaStats(import_stats, call_stats, di_stats, code_stats, tables, kept),
    )
