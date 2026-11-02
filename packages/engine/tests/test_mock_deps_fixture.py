"""The web mocks' dependency levels are the engine's deps over the mock's code graph (regenerate
them with ``uv run python scripts/mock_deps.py`` after changing the engine's deps or the graph
fixture).

The force layout's coordinates come out a little differently on another platform (its floating
point): they are compared exactly on macOS, where the mocks are made, and left out elsewhere
(Linux CI), where everything else must match."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import orjson

ROOT = Path(__file__).resolve().parents[3]
LAYOUT = frozenset({"x", "y"})
SCRIPT = ROOT / "scripts" / "mock_deps.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_deps", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _without(value: Any, keys: frozenset[str]) -> Any:
    """``value`` with ``keys`` left out wherever they are (the layout's numbers)."""
    if isinstance(value, dict):
        found = cast("dict[str, Any]", value)
        return {k: _without(v, keys) for k, v in found.items() if k not in keys}
    if isinstance(value, list):
        return [_without(v, keys) for v in cast("list[Any]", value)]
    return value


def test_the_web_mock_deps_are_the_engines_output() -> None:
    script = _script()
    mock = orjson.loads(script.OUT.read_bytes())
    built = script.build()
    if sys.platform == "darwin":
        assert mock == built
    else:
        assert _without(mock, LAYOUT) == _without(built, LAYOUT)
    # What the graph explorer's mock journeys rely on: a module cycle (the DSM's red cells), the
    # services, and third-party packages in use.
    modules = mock["levels"]["module"]
    assert modules["dsm"]["cycles"]
    assert any(cell["cycle"] for cell in modules["dsm"]["cells"])
    assert {n["id"] for n in mock["levels"]["service"]["deps"]["nodes"]} >= {"backend", "frontend"}
    assert any(p["name"] == "fastapi" for p in mock["packages"]["packages"])
