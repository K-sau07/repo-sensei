"""Write the web mocks' dependency levels (apps/web/src/mocks/fixtures/deps.json).

The mock's code graph (fixtures/graph.json, scripts/mock_graph.py) at each level the engine
aggregates it to (6.2: service, package, module, file, symbol), as the deps endpoints send it:
the nodes where the layout places them, the edges counted by type with their sample lines; the
level's DSM, cycles and coupling (6.3); each edge's evidence (the code edges behind it, for the
levels above symbols); and the third-party packages (6.5), the inventory read from the
snapshot's manifests and lockfiles (packages/engine/tests/fixtures/fastapi-template.tar.gz) and
joined with the graph's imports. Re-run after changing the engine's deps or the graph fixture::

    uv run python scripts/mock_deps.py
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Any

import orjson

from sensei_core.models import EdgeType, Provenance
from sensei_engine.deps import (
    LEVELS,
    MAX_FORCE,
    aggregate,
    coupling,
    cycles,
    dsm,
    evidence,
    grouping,
    layout,
)
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.ingest.frameworks import detect_frameworks, is_manifest
from sensei_engine.ingest.packages import inventory, is_lockfile
from sensei_engine.third_party import packages_in_use, usage

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "apps" / "web" / "src" / "mocks" / "fixtures"
GRAPH = FIXTURES / "graph.json"
TEMPLATE = ROOT / "packages" / "engine" / "tests" / "fixtures" / "fastapi-template.tar.gz"
OUT = FIXTURES / "deps.json"
USED_IN = 5  # the files listed per package, as the API lists them
EVIDENCE_LIMIT = 20  # lines kept per edge (its total is exact): the panel lists the first
NOTE = (
    "The mock snapshot's code graph (graph.json) at each dependency level, by the engine's deps "
    "(scripts/mock_deps.py): what /deps, /deps/dsm, /deps/cycles, /deps/edge-evidence and "
    "/deps/packages send, less the commit, which the mock adds."
)


def mock_graph() -> GraphSnapshot:
    """The mock's code graph (graph.json) as the engine holds one."""
    fixture: dict[str, list[dict[str, Any]]] = orjson.loads(GRAPH.read_bytes())
    nodes = [
        Node(
            n["symbol_key"], n["kind"], n["name"], n["qualified_name"], n["path"],
            n["start_line"], n["end_line"], n["lang"], n["lane"],
        )
        for n in fixture["nodes"]
    ]  # fmt: skip
    at = {node.symbol_key: i for i, node in enumerate(nodes)}
    edges: list[tuple[int, int, Edge]] = []
    for e in fixture["edges"]:
        at_line: dict[str, Any] = e["evidence"] or {}
        edge = Edge(
            EdgeType(e["type"]),
            e["confidence"],
            Provenance(e["provenance"]),
            at_line.get("path"),
            at_line.get("line"),
        )
        edges.append((at[e["src"]], at[e["dst"]], edge))
    return GraphSnapshot(nodes, edges, version="mock")


def _snapshot() -> tuple[list[str], dict[str, str], dict[str, str]]:
    """The template's paths, its manifests' text and its lockfiles' text."""
    paths: list[str] = []
    manifests: dict[str, str] = {}
    locks: dict[str, str] = {}
    with tarfile.open(TEMPLATE) as archive:
        for member in archive.getmembers():
            path = member.name.partition("/")[2]
            if not member.isfile() or not path:
                continue
            paths.append(path)
            if is_manifest(path) or is_lockfile(path):
                data = archive.extractfile(member)
                if data is None:
                    continue
                text = data.read().decode("utf-8", "replace")
                (locks if is_lockfile(path) else manifests)[path] = text
    return sorted(paths), manifests, locks


def _level(graph: GraphSnapshot, level: Any, roots: Any) -> dict[str, Any]:
    whole = aggregate(graph, level, roots)
    placed = layout(whole) if whole.nodes else {}
    clusters: dict[str, int] = {}
    nodes: list[dict[str, Any]] = []
    for key in sorted(whole.nodes):
        node = whole.nodes[key]
        x, y = placed.get(key, (0.0, 0.0))
        nodes.append({
            "id": key, "label": node.label, "layer": node.layer, "size": node.size,
            "cluster": node.cluster, "x": x, "y": y,
        })  # fmt: skip
        if node.cluster is not None:
            clusters[node.cluster] = clusters.get(node.cluster, 0) + 1
    edges = [
        {
            "source": src, "target": dst, "weight": edge.weight, "types": dict(edge.types),
            "confidence": edge.confidence,
            "samples": [{"path": p, "line": n} for p, n in edge.samples],
        }
        for (src, dst), edge in sorted(whole.edges.items())
    ]  # fmt: skip
    found = cycles(whole)
    matrix = dsm(whole, found)
    metrics = coupling(whole)
    cycles_out = [
        {
            "nodes": list(c.nodes),
            "edges": [{"source": a, "target": b, "weight": w} for a, b, w in c.edges],
        }
        for c in found
    ]
    out: dict[str, Any] = {
        "deps": {
            "level": level,
            "layout": "force" if len(whole.nodes) <= MAX_FORCE else "spiral",
            "total": len(whole.nodes),
            "truncated": False,
            "nodes": nodes,
            "edges": edges,
            "clusters": [{"id": k, "nodes": v} for k, v in sorted(clusters.items())],
        },
        "dsm": {
            "level": level,
            "cycles": cycles_out,
            "order": [
                {
                    "id": key,
                    "label": whole.nodes[key].label,
                    "layer": whole.nodes[key].layer,
                    "size": whole.nodes[key].size,
                    "afferent": metrics[key].afferent,
                    "efferent": metrics[key].efferent,
                    "instability": metrics[key].instability,
                }
                for key in matrix.order
            ],
            "cells": [{"row": r, "col": c, "count": n, "cycle": y} for r, c, n, y in matrix.cells],
        },
    }
    if level != "symbol":
        behind: dict[str, Any] = {}
        for src, dst in sorted(whole.edges):
            listed, total = evidence(graph, level, roots, src, dst, limit=EVIDENCE_LIMIT)
            behind[f"{src}\u0000{dst}"] = {
                "total": total,
                "edges": [
                    {
                        "source": e.src,
                        "target": e.dst,
                        "type": e.type,
                        "confidence": e.confidence,
                        "provenance": e.provenance,
                        "path": e.path,
                        "line": e.line,
                    }
                    for e in listed
                ],
            }
        out["evidence"] = behind
    return out


def _package(found: Any) -> dict[str, Any]:
    return {
        "ecosystem": found.ecosystem,
        "name": found.name,
        "versions": found.versions,
        "dev": found.dev,
        "declared": [
            {
                "manifest": str(d.get("manifest", "")),
                "root": str(d.get("root", ".")),
                "spec": d.get("spec"),
                "version": d.get("version"),
                "dev": bool(d.get("dev")),
            }
            for d in found.declared
        ],
        "files": len(found.usage.files),
        "imports": found.usage.imports,
        "used_in": [
            {"path": path, "imports": n}
            for path, n in sorted(found.usage.files.items(), key=lambda x: (-x[1], x[0]))[:USED_IN]
        ],
    }


def build() -> dict[str, Any]:
    graph = mock_graph()
    paths, manifests, locks = _snapshot()
    frameworks = [f.as_json() for f in detect_frameworks(manifests)]
    roots = grouping(paths, frameworks)
    declared = [p.as_json() for p in inventory(manifests, locks)]
    found, undeclared = packages_in_use(declared, usage(graph))
    return {
        "note": NOTE,
        "levels": {level: _level(graph, level, roots) for level in LEVELS},
        "packages": {
            "inventoried": True,
            "packages": [_package(p) for p in found],
            "undeclared": [_package(p) for p in undeclared],
        },
    }


def main() -> None:
    # Compact, as written (in .prettierignore): indented, it would be over the size check.
    OUT.write_bytes(orjson.dumps(build()) + b"\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
