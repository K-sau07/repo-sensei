"""Third-party packages in use (6.5): declared packages joined with the code's imports."""

from __future__ import annotations

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node
from sensei_engine.third_party import packages_in_use, usage


def _node(key: str, kind: NodeKind, path: str | None) -> Node:
    return Node(key, kind.value, key, key, path, 1, 2, "python")


def _graph() -> GraphSnapshot:
    nodes = [
        _node("py app/main.py", NodeKind.MODULE, "app/main.py"),
        _node("py app/config.py", NodeKind.MODULE, "app/config.py"),
        _node("ts web/page.tsx", NodeKind.MODULE, "web/page.tsx"),
        _node("package py:fastapi", NodeKind.PACKAGE, None),
        _node("external_symbol py:fastapi.routing/APIRouter", NodeKind.EXTERNAL_SYMBOL, None),
        _node("external_symbol py:yaml/safe_load", NodeKind.EXTERNAL_SYMBOL, None),
        _node("package py:os", NodeKind.PACKAGE, None),
        _node("package py:requests", NodeKind.PACKAGE, None),
        _node("external_symbol npm:@tanstack/react-query/useQuery", NodeKind.EXTERNAL_SYMBOL, None),
        _node("package npm:node:fs", NodeKind.PACKAGE, None),
    ]
    at = {n.symbol_key: i for i, n in enumerate(nodes)}
    imports = Edge(EdgeType.IMPORTS, 1.0, Provenance.IMPORT_RESOLVED, None, 1)
    pairs = [
        ("py app/main.py", "package py:fastapi"),
        ("py app/main.py", "external_symbol py:fastapi.routing/APIRouter"),
        ("py app/config.py", "package py:fastapi"),
        ("py app/config.py", "external_symbol py:yaml/safe_load"),
        ("py app/config.py", "package py:os"),
        ("py app/main.py", "package py:requests"),
        ("ts web/page.tsx", "external_symbol npm:@tanstack/react-query/useQuery"),
        ("ts web/page.tsx", "package npm:node:fs"),
    ]
    return GraphSnapshot(nodes, [(at[a], at[b], imports) for a, b in pairs])


DECLARED = [
    {"ecosystem": "pypi", "name": "fastapi", "version": "0.115.6", "dev": False,
     "manifest": "pyproject.toml", "root": ".", "spec": ">=0.115"},
    {"ecosystem": "pypi", "name": "pyyaml", "version": "6.0.2", "dev": False,
     "manifest": "pyproject.toml", "root": ".", "spec": None},
    {"ecosystem": "pypi", "name": "pytest", "version": "8.3.4", "dev": True,
     "manifest": "pyproject.toml", "root": ".", "spec": None},
    {"ecosystem": "npm", "name": "@tanstack/react-query", "version": "5.62.0", "dev": False,
     "manifest": "web/package.json", "root": "web", "spec": "^5"},
]  # fmt: skip


def test_declared_packages_are_joined_with_the_imports() -> None:
    used = usage(_graph())
    assert used[("pypi", "fastapi")].files == {"app/main.py": 2, "app/config.py": 1}
    declared, undeclared = packages_in_use(DECLARED, used)
    by_name = {p.name: p for p in declared}
    assert declared[0].name == "fastapi"  # the most used first
    assert by_name["fastapi"].versions == ["0.115.6"]
    assert by_name["fastapi"].usage.imports == 3
    assert set(by_name["pyyaml"].usage.files) == {"app/config.py"}  # "yaml" is PyYAML
    assert set(by_name["@tanstack/react-query"].usage.files) == {"web/page.tsx"}
    assert by_name["pytest"].usage.imports == 0
    assert by_name["pytest"].dev
    # Imported, declared nowhere: requests (os and node:fs are built in).
    assert [(p.ecosystem, p.name) for p in undeclared] == [("pypi", "requests")]
