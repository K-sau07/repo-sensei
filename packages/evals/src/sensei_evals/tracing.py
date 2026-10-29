"""The ``trace`` tracer for CrossLayerBench (3.13.5): 3.12's ``trace_feature`` on a bench
repository's checkout, linked in this process as the index would link it, with no database and
no model call.

:func:`link_checkout` runs the stages' work over the checkout: discovery (each file's language
and role), the definitions the symbols stage would save, the graph stage's resolvers (the CODE
layer) and the links stage's linkers (the LINKS layer, routes read statically: no sandbox),
into one graph in memory, each node in its file's lane, each edge the surest of its kind on its
line (as the writer keeps them). :class:`TraceTracer` traces every flow from its entry (the
gold's first hop: its definition in the graph, else its file) and hands the paths to the bench.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node, lane
from sensei_engine.graph.stage import extract_files, resolve_graph, resolver_for
from sensei_engine.graph.trace import TraceError, table_model, trace_feature
from sensei_engine.graph.trace import TraceHop as EngineHop
from sensei_engine.graph.trace import TracePath as EnginePath
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.ingest.classify import Flag
from sensei_engine.ingest.discover import discover
from sensei_engine.link import http_calls
from sensei_engine.link.stage import env_files, extract_links, links_language, resolve_links
from sensei_engine.parse.parser import TIMEOUT_SECONDS, parse_file
from sensei_engine.parse.pool import Failed, run_parallel
from sensei_engine.parse.stage import parseable, read_source
from sensei_engine.symbols.extract import extract_symbols
from sensei_evals.crosslayer import Flow, TraceHop, TracePath, TracerContext

MAX_FILES = 200_000
UNREAD_FLAGS = frozenset({Flag.BINARY, Flag.TOO_LARGE, Flag.LFS_POINTER})


def _definitions(request: tuple[str, str, str]) -> tuple[Definition, ...]:
    """A file's definitions, as the symbols stage extracts them (the pool's work)."""
    root, path, language = request
    parsed = parse_file(path, read_source(Path(root), path), language=language)
    if not parsed.ok:
        return ()
    return tuple(
        Definition(s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line)
        for s in extract_symbols(parsed, language)
    )


def _graph(
    definitions: dict[str, tuple[Definition, ...]],
    languages: dict[str, str],
    roles: dict[str, str],
    nodes: Iterable[GraphNode],
    edges: Iterable[GraphEdge],
) -> GraphSnapshot:
    found: dict[str, Node] = {}
    for path, defined in definitions.items():
        for d in defined:
            found[d.symbol_key] = Node(
                d.symbol_key, d.kind, d.name, d.qualified_name, path, d.start_line, d.end_line,
                languages.get(path, ""), lane(d.kind, path, roles.get(path)),
            )  # fmt: skip
    for n in nodes:
        found.setdefault(
            n.key,
            Node(
                n.key, n.kind.value, n.name, n.qualified_name or n.name, n.path, n.start_line,
                n.end_line, n.lang, lane(n.kind.value, n.path, roles.get(n.path or "")),
            ),
        )  # fmt: skip
    order = sorted(found)
    at = {key: i for i, key in enumerate(order)}
    kept: dict[tuple[int, int, str, int | None], Edge] = {}
    for e in edges:
        if e.src not in at or e.dst not in at:
            continue
        confidence = e.confidence if e.confidence is not None else confidence_of(e.provenance)
        key = (at[e.src], at[e.dst], e.type.value, e.evidence_line)
        held = kept.get(key)
        if held is None or confidence > held.confidence:
            kept[key] = Edge(e.type, confidence, e.provenance, e.evidence_path, e.evidence_line)
    return GraphSnapshot(
        [found[key] for key in order], [(a, b, edge) for (a, b, _, _), edge in kept.items()]
    )


def link_checkout(root: Path, *, workers: int | None = None) -> GraphSnapshot:
    """The checkout's graph, both layers, as the index would hold it (see the module)."""
    every = discover(root, max_files=MAX_FILES).files
    files = [f for f in every if f.language is not None and not UNREAD_FLAGS.intersection(f.flags)]
    languages = {f.path: f.language for f in files if f.language is not None}
    roles = {f.path: f.role.value for f in every}
    paths = sorted(f.path for f in every)
    readable = [(p, lang) for p, lang in languages.items() if parseable(lang)]
    definitions: dict[str, tuple[Definition, ...]] = {}
    for (_, path, _), result in run_parallel(
        _definitions,
        [(str(root), p, lang) for p, lang in readable],
        workers=workers,
        item_timeout=TIMEOUT_SECONDS,
    ):
        if not isinstance(result, Failed) and result:
            definitions[path] = result
    texts = {
        p: read_source(root, p).decode("utf-8", "replace")
        for p in paths
        if is_config(p) or p in env_files(paths)
    }
    project = Project(
        frozenset(paths), {p: t for p, t in texts.items() if is_config(p)}, definitions
    )
    facts: dict[str, dict[str, object]] = {}
    code_files = [(p, lang) for p, lang in languages.items() if resolver_for(lang) is not None]
    for found in extract_files(root, code_files, workers=workers):
        if found.facts is not None:
            facts.setdefault(found.resolver, {})[found.path] = found.facts
    code = resolve_graph(project, facts)
    envs = http_calls.env_values(project, {p: texts[p] for p in env_files(paths)})
    link_files = [(p, lang) for p, lang in languages.items() if links_language(lang)]
    linked = {f.path: f for f in extract_links(root, link_files, envs, workers=workers)}
    links = resolve_links(project, linked)
    return _graph(
        definitions, languages, roles, [*code.nodes, *links.nodes], [*code.edges, *links.edges]
    )


def entry_of(graph: GraphSnapshot, flow: Flow) -> str:
    """Where a flow's trace starts: the gold's first hop's definition, else its file."""
    first = flow.hops[0]
    if first.symbol:
        here = sorted((n for n in graph.nodes if n.path == first.path), key=lambda n: n.start_line)
        for qualified in (True, False):
            for node in here:
                if (node.qualified_name if qualified else node.name) == first.symbol:
                    return node.symbol_key
    return first.path


def _bench_hop(graph: GraphSnapshot, hop: EngineHop) -> TraceHop:
    node = hop.node
    model = table_model(graph, node) if node.kind == "table" else None
    return TraceHop(
        kind=node.kind,
        name=node.qualified_name,
        path=node.path if node.start_line > 0 else None,
        lines=(node.start_line, node.end_line) if node.start_line > 0 else None,
        confidence=hop.edge.confidence if hop.edge is not None else None,
        layer=hop.layer,
        model=(model.path, (model.start_line, model.end_line))
        if model is not None and model.path is not None
        else None,
    )


def bench_paths(graph: GraphSnapshot, paths: Sequence[EnginePath]) -> list[TracePath]:
    """The engine's paths as the bench scores them (a table placed at its ORM class), each
    return leg kept apart (not scored)."""
    found: list[TracePath] = []
    for path in paths:
        down = path.hops[: path.return_index]
        back = path.hops[len(down) :]
        breaks = tuple(f"after hop {b.after_hop}: {b.reason}" for b in path.breaks)
        found.append(
            TracePath(
                tuple(_bench_hop(graph, hop) for hop in down),
                path.confidence,
                breaks,
                returns=tuple(_bench_hop(graph, hop) for hop in back),
            )
        )
    return found


class TraceTracer:
    """``trace_feature`` on the checkout (``--checkout``), linked once for the run;
    ``--opt min_conf=<0..1>`` drops less sure edges."""

    name = "trace"

    def __init__(self, graph: GraphSnapshot, *, min_conf: float = 0.0) -> None:
        self.graph = graph
        self.min_conf = min_conf

    @classmethod
    def build(cls, context: TracerContext) -> TraceTracer:
        if context.checkout is None:
            msg = "the trace tracer needs the repository's checkout (--checkout)"
            raise ValueError(msg)
        min_conf = float(context.options.get("min_conf", "0"))
        return cls(link_checkout(context.checkout), min_conf=min_conf)

    def trace(self, flow: Flow) -> list[TracePath]:
        try:
            result = trace_feature(self.graph, entry_of(self.graph, flow), min_conf=self.min_conf)
        except TraceError:
            return []
        return bench_paths(self.graph, result.paths)
