"""The web mocks' symbols are the engine's symbols of the files the mock serves, as the API sends
them (regenerate them with ``uv run python scripts/mock_symbols.py`` after changing the
extractor)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import orjson

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "mock_symbols.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_symbols", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_web_mock_is_the_extractors_output() -> None:
    script = _script()
    mock = orjson.loads(script.OUT.read_bytes())
    assert mock == script.build()
    assert len({s["symbol_key"] for s in mock["symbols"]}) == len(mock["symbols"])
