"""The web mocks' dependency levels are the engine's deps over the mock's code graph (regenerate
them with ``uv run python scripts/mock_deps.py`` after changing the engine's deps or the graph
fixture)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import orjson

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "mock_deps.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_deps", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_web_mock_deps_are_the_engines_output() -> None:
    script = _script()
    mock = orjson.loads(script.OUT.read_bytes())
    assert mock == script.build()
    # What the graph explorer's mock journeys rely on: a module cycle (the DSM's red cells), the
    # services, and third-party packages in use.
    modules = mock["levels"]["module"]
    assert modules["dsm"]["cycles"]
    assert any(cell["cycle"] for cell in modules["dsm"]["cells"])
    assert {n["id"] for n in mock["levels"]["service"]["deps"]["nodes"]} >= {"backend", "frontend"}
    assert any(p["name"] == "fastapi" for p in mock["packages"]["packages"])
