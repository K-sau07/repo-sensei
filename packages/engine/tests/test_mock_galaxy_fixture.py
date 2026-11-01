"""The web mocks' semantic galaxy is the engine's layout of the mock files' content (regenerate
it with ``uv run python scripts/mock_galaxy.py`` after changing the galaxy's layout)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import orjson

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "mock_galaxy.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_galaxy", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_web_mock_galaxy_is_the_engines_layout() -> None:
    script = _script()
    mock = orjson.loads(script.OUT.read_bytes())
    assert mock == script.build()
    # What the map's mock journeys rely on: two named clusters over every star.
    assert len(mock["clusters"]) == 2
    assert sum(c["files"] for c in mock["clusters"]) == len(mock["stars"])
