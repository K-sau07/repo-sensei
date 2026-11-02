"""The web mocks' semantic galaxy is the engine's layout of the mock files' content (regenerate
it with ``uv run python scripts/mock_galaxy.py`` after changing the galaxy's layout).

The layout's coordinates, and so which of the two clusters a file near their border falls in,
come out a little differently on another platform (its floating point): they are compared
exactly on macOS, where the mocks are made, and left out elsewhere (Linux CI), where everything
else must match."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import orjson

ROOT = Path(__file__).resolve().parents[3]
LAYOUT = frozenset({"x", "y", "z", "cluster", "files"})
SCRIPT = ROOT / "scripts" / "mock_galaxy.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_galaxy", SCRIPT)
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


def test_the_web_mock_galaxy_is_the_engines_layout() -> None:
    script = _script()
    mock = orjson.loads(script.OUT.read_bytes())
    built = script.build()
    if sys.platform == "darwin":
        assert mock == built
    else:
        assert _without(mock, LAYOUT) == _without(built, LAYOUT)
    # What the map's mock journeys rely on: two named clusters over every star.
    assert len(mock["clusters"]) == 2
    assert sum(c["files"] for c in mock["clusters"]) == len(mock["stars"])
