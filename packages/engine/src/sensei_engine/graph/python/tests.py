"""Python tests as graph facts (3.5): each test function a ``test`` node, with ``TESTS`` edges to
the repository's definitions it calls.

A test is what pytest collects: a function named ``test*`` (or a method of a ``Test*`` class)
in a test file (``test_*.py``, ``*_test.py``, or anything under a ``tests`` directory, which is
how ``conftest.py`` projects lay them out). Its node is ``test <path>:<line>``, named by its
qualified name, owned by its file, and ``attrs.function`` is its definition's key. What it tests
is what 3.4's ``CALLS`` edges say it calls, a definition of the repository's (not a package's:
``client.get``, ``assert``-helpers), at the call's line and provenance, at most 0.7: being called
in a test is a heuristic for being tested.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass

from sensei_core.models import EdgeType, NodeKind
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.writer import GraphEdge, GraphNode

TESTS_CONFIDENCE = 0.7


@dataclass(slots=True)
class TestStats:
    __test__ = False  # not a pytest test class

    tests: int = 0
    edges: int = 0


def is_test_file(path: str) -> bool:
    name = posixpath.basename(path)
    if not name.endswith(".py"):
        return False
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "tests" in posixpath.dirname(path).split("/")
    )


def is_test(definition: Definition) -> bool:
    if definition.kind not in ("function", "method") or not definition.name.startswith("test"):
        return False
    containers = definition.qualified_name.split(".")[:-1]
    return all(name.startswith("Test") for name in containers)


def node_key(path: str, line: int) -> str:
    return f"test {path}:{line}"


def resolve(
    project: Project, call_edges: Iterable[GraphEdge]
) -> tuple[list[GraphNode], list[GraphEdge], TestStats]:
    """The project's Python test nodes and their ``TESTS`` edges (see the module)."""
    stats = TestStats()
    tests: dict[str, GraphNode] = {}
    for path in sorted(project.definitions):
        if not is_test_file(path):
            continue
        for definition in project.definitions[path]:
            if is_test(definition):
                key = node_key(path, definition.start_line)
                tests[definition.symbol_key] = GraphNode(
                    key=key,
                    kind=NodeKind.TEST,
                    name=definition.qualified_name,
                    path=path,
                    start_line=definition.start_line,
                    end_line=definition.end_line,
                    qualified_name=definition.qualified_name,
                    lang="python",
                    attrs={"function": definition.symbol_key},
                    owner=path,
                )
    edges: list[GraphEdge] = []
    for edge in call_edges:
        node = tests.get(edge.src)
        if node is None or edge.type is not EdgeType.CALLS:
            continue
        if edge.dst.startswith(("package ", "external_symbol ")):
            continue
        confidence = edge.confidence
        if confidence is None:
            confidence = confidence_of(edge.provenance)
        edges.append(
            GraphEdge(
                src=node.key,
                dst=edge.dst,
                type=EdgeType.TESTS,
                provenance=edge.provenance,
                confidence=min(confidence, TESTS_CONFIDENCE),
                evidence_path=edge.evidence_path,
                evidence_line=edge.evidence_line,
                owner=node.path,
            )
        )
    stats.tests, stats.edges = len(tests), len(edges)
    return list(tests.values()), edges, stats
