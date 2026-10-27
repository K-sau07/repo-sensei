"""Python's graph facts: imports (3.2), then calls, references and bases (3.4), FastAPI's
dependencies and the tests (3.5).

The graph stage calls :func:`extract` on each Python file in the parse pool and :func:`resolve`
on them all; both run the two resolvers over one set of import tables.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sensei_engine.graph.project import Project
from sensei_engine.graph.python import calls, di, imports, tests
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.parse.parser import ParseResult

LANGUAGES = frozenset({"python"})


@dataclass(frozen=True, slots=True)
class PyFileFacts:
    imports: imports.PyFacts = field(default_factory=imports.PyFacts)
    calls: calls.CallFacts = field(default_factory=calls.CallFacts)
    di: di.DiFacts = field(default_factory=di.DiFacts)


@dataclass(slots=True)
class PyStats:
    imports: imports.ImportStats
    calls: calls.CallStats
    tables: dict[str, dict[str, str]]  # module key: its import table (what annotate keeps)
    di: di.DiStats = field(default_factory=di.DiStats)
    tests: tests.TestStats = field(default_factory=tests.TestStats)

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


def extract(parsed: ParseResult) -> PyFileFacts | None:
    """A Python file's imports, scopes and sites (None: another language, or no parse)."""
    if parsed.language not in LANGUAGES or not parsed.ok:
        return None
    return PyFileFacts(imports.extract(parsed), calls.extract(parsed), di.extract(parsed))


def resolve(
    project: Project, facts: Mapping[str, PyFileFacts]
) -> tuple[list[GraphNode], list[GraphEdge], PyStats]:
    """The project's Python graph: imports, calls, references and bases, and their externals."""
    import_facts = {path: f.imports for path, f in facts.items()}
    resolver = imports.Resolver(project, import_facts)
    import_nodes, import_edges, import_stats = imports.resolve(project, import_facts, resolver)
    call_facts = {path: f.calls for path, f in facts.items()}
    call_nodes, call_edges, call_stats = calls.resolve_calls(project, call_facts, resolver)
    di_nodes, di_edges, di_stats = di.resolve(
        project,
        {path: f.di for path, f in facts.items()},
        calls.CallResolver(project, call_facts, resolver),
        resolver,
    )
    test_nodes, test_edges, test_stats = tests.resolve(project, call_edges)
    nodes = {node.key: node for node in [*import_nodes, *call_nodes, *di_nodes, *test_nodes]}
    tables = {
        key: {local: target.key for local, target in resolver.table(path).items()}
        for path in sorted(facts)
        if (key := project.module_key(path)) is not None
    }
    return (
        list(nodes.values()),
        [*import_edges, *call_edges, *di_edges, *test_edges],
        PyStats(import_stats, call_stats, tables, di_stats, test_stats),
    )
