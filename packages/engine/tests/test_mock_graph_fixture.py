"""The web mocks' code graph is the engine's graph of the files the mock serves (regenerate it
with ``uv run python scripts/mock_graph.py`` after changing a resolver)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import orjson

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "mock_graph.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_graph", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_web_mock_graph_is_the_resolvers_output() -> None:
    script = _script()
    mock = orjson.loads(script.OUT.read_bytes())
    assert mock == script.build()
    keys = {n["symbol_key"] for n in mock["nodes"]}
    assert len(keys) == len(mock["nodes"])
    assert all(e["src"] in keys and e["dst"] in keys for e in mock["edges"])
    # What the code viewer's mock journeys rely on: a handler calling into crud, an endpoint.
    calls = {(e["src"], e["dst"]) for e in mock["edges"] if e["type"] == "calls"}
    assert (
        "py backend/app/api/routes/users.py/create_user().",
        "py backend/app/crud.py/create_user().",
    ) in calls
