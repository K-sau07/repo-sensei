"""The graph in memory on a large repository (3.6.7): Langflow's code graph, built as the graph
stage builds it (in this process, no database), answers ``neighbors`` at p95 under 50 ms, and so
does the search's PageRank leg.

Needs a checkout of Langflow: ``SENSEI_LANGFLOW_CHECKOUT``, else the eval checkout
(``.cache/eval-repos/langflow``); skipped without one. Building the graph takes a minute or two.
"""

from __future__ import annotations

import os
import random
import statistics
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from sensei_core.models import SYMBOL_KINDS
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.graph.stage import extract_files, resolve_graph, resolver_for
from sensei_engine.ingest.discover import discover
from sensei_engine.parse.stage import parse_files, parseable, read_source

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[3]
CHECKOUT = Path(
    os.environ.get("SENSEI_LANGFLOW_CHECKOUT") or ROOT / ".cache" / "eval-repos" / "langflow"
)
BUDGET_MS = 50.0
ASKED = 300  # definitions asked about, at random (seeded)


def _graph(root: Path) -> GraphSnapshot:
    """The checkout's code graph, as the stage would write it, read into memory."""
    found = discover(root, max_files=200_000)
    paths = frozenset(f.path for f in found.files)
    languages = [(f.path, f.language) for f in found.files if f.language is not None]
    definitions: dict[str, tuple[Definition, ...]] = {}
    nodes: dict[str, Node] = {}
    for parsed in parse_files(root, [(p, lang) for p, lang in languages if parseable(lang)]):
        kept = [s for s in parsed.symbols if s.kind in SYMBOL_KINDS]
        definitions[parsed.path] = tuple(
            Definition(s.symbol_key, s.name, s.qualified_name, s.kind, s.start_line, s.end_line)
            for s in kept
        )
        for s in kept:
            nodes[s.symbol_key] = Node(
                s.symbol_key, s.kind, s.name, s.qualified_name, parsed.path,
                s.start_line, s.end_line, parsed.language,
            )  # fmt: skip
    configs = {p: read_source(root, p).decode("utf-8", "replace") for p in paths if is_config(p)}
    facts: dict[str, dict[str, object]] = {}
    for file in extract_files(root, [(p, lang) for p, lang in languages if resolver_for(lang)]):
        if file.facts is not None:
            facts.setdefault(file.resolver, {})[file.path] = file.facts
    build = resolve_graph(Project(paths, configs, definitions), facts)
    for node in build.nodes:
        nodes.setdefault(
            node.key,
            Node(
                node.key, node.kind.value, node.name, node.qualified_name or node.name,
                node.path, node.start_line, node.end_line, node.lang,
            ),
        )  # fmt: skip
    index = {key: i for i, key in enumerate(nodes)}
    edges = [
        (
            index[e.src],
            index[e.dst],
            Edge(
                e.type,
                e.confidence if e.confidence is not None else confidence_of(e.provenance),
                e.provenance,
                e.evidence_path,
                e.evidence_line,
            ),
        )
        for e in build.edges
        if e.src in index and e.dst in index
    ]
    return GraphSnapshot(list(nodes.values()), edges)


def _p95_ms(ask: Callable[[str], object], keys: list[str]) -> float:
    took: list[float] = []
    for key in keys:
        started = time.perf_counter()
        ask(key)
        took.append((time.perf_counter() - started) * 1000)
    return statistics.quantiles(took, n=20)[18]


@pytest.mark.skipif(not CHECKOUT.is_dir(), reason=f"no Langflow checkout at {CHECKOUT}")
def test_neighbours_and_pagerank_answer_within_the_budget_on_langflow() -> None:
    graph = _graph(CHECKOUT)
    assert len(graph.edges) > 100_000  # the whole repository linked, not a corner of it
    defined = [n.symbol_key for n in graph.nodes if n.kind in SYMBOL_KINDS]
    keys = random.Random(0).sample(defined, min(ASKED, len(defined)))  # noqa: S311 - not a secret
    neighbours = _p95_ms(
        lambda key: graph.neighbors(key, direction="both", depth=2, limit=200), keys
    )
    pagerank = _p95_ms(lambda key: graph.personalized_pagerank({key: 1.0}), keys)
    print(f"neighbors p95 {neighbours:.1f} ms, pagerank p95 {pagerank:.1f} ms")
    assert neighbours < BUDGET_MS
    assert pagerank < BUDGET_MS
