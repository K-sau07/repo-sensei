"""Write the web mocks' code graph (apps/web/src/mocks/fixtures/graph.json).

The mock serves the content of a selection of fastapi/full-stack-fastapi-template's files at
b1125d8 (fixtures/browse-content.json); this builds their graph with the engine, as indexing
does: the Python resolver's imports, calls, references, bases and dependencies (3.2, 3.4, 3.5),
the JavaScript and TypeScript resolver's imports, calls, renders and hooks (3.3, 3.5), the
routes' endpoints and handlers (3.8, statically: no sandbox here) and the data layer's models,
tables and reads and writes (3.11). Resolution reads the snapshot's config files (tsconfig,
package.json, pyproject) as indexing does, from the engine's copy of the same commit
(packages/engine/tests/fixtures/fastapi-template.tar.gz), since the mock serves only some files'
content. Nodes are the files' definitions (with their docstrings, which ``/symbols/at`` returns)
and the graph's own nodes, each with its lane as the graph endpoints send it; edges as the graph
snapshot holds them (3.6). An engine test checks the file still matches the resolvers, so re-run
this after changing them::

    uv run python scripts/mock_graph.py
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Any

import orjson

from sensei_engine.graph import python
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project, is_config
from sensei_engine.graph.python.imports import extract as extract_imports
from sensei_engine.graph.writer import GraphEdge, GraphNode
from sensei_engine.imports.ecma import graph as ecma
from sensei_engine.ingest.languages import detect_language
from sensei_engine.link import orm
from sensei_engine.link.fastapi import endpoint_facts, scan_routes, static_routes
from sensei_engine.parse.parser import ParseResult, parse_file
from sensei_engine.parse.stage import parseable
from sensei_engine.search.layers import lane_of, layer_of
from sensei_engine.symbols.extract import Symbol, extract_symbols

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "apps" / "web" / "src" / "mocks" / "fixtures"
CONTENT = FIXTURES / "browse-content.json"
TREE = FIXTURES / "browse-tree.json"
TEMPLATE = ROOT / "packages" / "engine" / "tests" / "fixtures" / "fastapi-template.tar.gz"
OUT = FIXTURES / "graph.json"
NOTE = (
    "The code graph of the mock snapshot's files whose content is in browse-content.json, from "
    "the engine's resolvers by scripts/mock_graph.py: nodes (definitions and the graph's own, "
    "each with its lane) and edges as the graph endpoints send them. Lines are 1-based and "
    "inclusive."
)
# The lanes of the graph's own nodes, as the graph snapshot gives them (a definition's comes from
# its file's role; anything else, an external, is "other").
NODE_LANES = {
    **dict.fromkeys(("api_endpoint", "graphql_operation", "graphql_field", "rpc_method"), "api"),
    **dict.fromkeys(("orm_model", "table", "column", "sql_statement"), "db"),
}


def _node(node: GraphNode) -> dict[str, Any]:
    return {
        "symbol_key": node.key,
        "kind": node.kind.value,
        "name": node.name,
        "qualified_name": node.qualified_name or node.name,
        "path": node.path,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "lang": node.lang,
        "lane": NODE_LANES.get(node.kind.value, "other"),
        "docstring": None,
    }


def _edge(edge: GraphEdge) -> dict[str, Any]:
    confidence = edge.confidence if edge.confidence is not None else confidence_of(edge.provenance)
    return {
        "src": edge.src,
        "dst": edge.dst,
        "type": edge.type.value,
        "provenance": edge.provenance.value,
        "confidence": round(confidence, 4),
        "evidence": (
            {"path": edge.evidence_path, "line": edge.evidence_line}
            if edge.evidence_path is not None
            else None
        ),
    }


def _configs(paths: set[str]) -> dict[str, str]:
    """The snapshot's config files, from the template archive (entries under its folder)."""
    found: dict[str, str] = {}
    with tarfile.open(TEMPLATE) as archive:
        for member in archive.getmembers():
            path = member.name.partition("/")[2]
            if member.isfile() and path in paths and is_config(path):
                data = archive.extractfile(member)
                if data is not None:
                    found[path] = data.read().decode("utf-8", "replace")
    return found


def build() -> dict[str, Any]:
    content: dict[str, str] = orjson.loads(CONTENT.read_bytes())["files"]
    tree = orjson.loads(TREE.read_bytes())
    roles: dict[str, str] = {f["path"]: f["role"] for f in tree["files"]}
    parsed: dict[str, ParseResult] = {}
    symbols: dict[str, list[Symbol]] = {}
    for path, text in sorted(content.items()):
        source = text.encode()
        language = detect_language(path, source[:256])
        if language is None or not parseable(language):
            continue
        result = parse_file(path, source, language=language)
        parsed[path] = result
        symbols[path] = extract_symbols(result, language)
    definitions = {
        path: [
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in found
        ]
        for path, found in symbols.items()
    }
    configs = _configs(set(roles))
    project = Project(frozenset(roles), configs, definitions)
    py = {path: result for path, result in parsed.items() if result.language == "python"}

    nodes: dict[str, dict[str, Any]] = {}
    for path, found in symbols.items():
        for s in found:
            nodes[s.symbol_key] = {
                "symbol_key": s.symbol_key,
                "kind": s.kind.value,
                "name": s.name,
                "qualified_name": s.qualified_name,
                "path": path,
                "start_line": s.start_line,
                "end_line": s.end_line,
                "lang": parsed[path].language or "",
                "lane": lane_of(layer_of(roles.get(path), s.kind.value, path)).value,
                "docstring": s.docstring,
            }
    facts = {path: f for path, result in py.items() if (f := python.extract(result)) is not None}
    code_nodes, code_edges, _ = python.resolve(project, facts)
    ts_facts = {
        path: f for path, result in parsed.items() if (f := ecma.extract(result)) is not None
    }
    ts_nodes, ts_edges, _ = ecma.resolve(project, ts_facts)
    routes = static_routes(
        project,
        {path: scan_routes(result, definitions[path]) for path, result in py.items()},
        {path: extract_imports(result) for path, result in py.items()},
    )
    endpoints = endpoint_facts(project, routes, None)
    data = orm.link(orm.scan(result, symbols[path]) for path, result in py.items())
    for node in [*code_nodes, *ts_nodes, *endpoints.nodes, *data.nodes]:
        nodes.setdefault(node.key, _node(node))
    edges = [
        _edge(edge)
        for edge in [*code_edges, *ts_edges, *endpoints.edges, *data.edges]
        if edge.src in nodes and edge.dst in nodes  # as the writer keeps them
    ]
    edges.sort(key=lambda e: (e["src"], e["type"], e["dst"], str(e["evidence"])))
    return {
        "source": tree["source"],
        "note": NOTE,
        "nodes": sorted(nodes.values(), key=lambda n: n["symbol_key"]),
        "edges": edges,
    }


def main() -> None:
    OUT.write_bytes(orjson.dumps(build(), option=orjson.OPT_INDENT_2) + b"\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
