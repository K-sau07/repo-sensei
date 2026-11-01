"""Write the web mocks' impact fixture (apps/web/src/mocks/fixtures/impact.json).

The mock's impact endpoint (6.8) walks the mock code graph (fixtures/graph.json) as the engine
does (6.4: :mod:`sensei_engine.impact`), in TypeScript. This writes what it can't work out
cheaply, the graph's pages (``screens``: components nothing else renders, from the trace
index), and, for a few changes, the engine's own answer, which a web test checks the mock's
against. Re-run after changing the engine's impact or the graph fixture::

    uv run python scripts/mock_impact.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import orjson

from sensei_engine.graph.trace import pages
from sensei_engine.impact import CATEGORIES, Affected, changed_nodes, impact

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "apps" / "web" / "src" / "mocks" / "fixtures"
OUT = FIXTURES / "impact.json"
# Changes across the layers: a service, a model, a dependency, a hook, an endpoint, a table.
CASES = (
    "py backend/app/crud.py/create_user().",
    "py backend/app/models.py/User#",
    "py backend/app/api/deps.py/get_current_user().",
    "ts frontend/src/hooks/useAuth.ts/useAuth().",
    "py backend/app/core/security.py/verify_password().",
    "table user",
)
NOTE = (
    "For the mock impact endpoint (scripts/mock_impact.py): the mock graph's pages, and the "
    "engine's impact of a few changes, which the mock's own walk is checked against."
)


def _graph() -> Any:
    """The mock's graph, built as scripts/mock_deps.py builds it."""
    spec = importlib.util.spec_from_file_location("mock_deps", ROOT / "scripts" / "mock_deps.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("scripts/mock_deps.py is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.mock_graph()


def _affected(item: Affected) -> dict[str, Any]:
    return {
        "key": item.node.symbol_key,
        "score": round(item.score, 4),
        "distance": item.distance,
        "via": item.via,
        "edge": item.edge.value if item.edge is not None else None,
    }


def build() -> dict[str, Any]:
    graph = _graph()
    cases: dict[str, Any] = {}
    for key in CASES:
        result = impact(graph, changed_nodes(graph, symbols=[key]))
        cases[key] = {
            name: [_affected(a) for a in result.affected.get(name, [])] for name in CATEGORIES
        }
    return {"note": NOTE, "screens": sorted(pages(graph)), "cases": cases}


def main() -> None:
    OUT.write_bytes(orjson.dumps(build(), option=orjson.OPT_INDENT_2) + b"\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
