"""The web mocks' impact fixture is the engine's (regenerate it with ``uv run python
scripts/mock_impact.py`` after changing the engine's impact, its pages, or the graph fixture); a
web test checks the mock's own walk against its cases."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import orjson

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "mock_impact.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_impact", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_web_mock_impact_is_the_engines() -> None:
    script = _script()
    mock = orjson.loads(script.OUT.read_bytes())
    assert mock == script.build()
    # What the impact page's mock journeys rely on: a change reaching routes and a table.
    user = mock["cases"]["py backend/app/models.py/User#"]
    assert user["routes"]
    assert [t["key"] for t in user["tables"]] == ["table user"]
