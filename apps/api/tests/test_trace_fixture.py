"""The web mocks' traces are the engine's traces of the template, as ``GET …/trace`` sends them
(regenerate them with ``uv run python scripts/mock_trace.py`` after changing the tracer, a linker
or the router's models)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import orjson

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "mock_trace.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_trace", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_web_mock_traces_are_the_tracers_output() -> None:
    script = _script()
    mock = orjson.loads(script.OUT.read_bytes())
    assert mock == script.build()
    # What the trace view's mock journeys rely on: the Login page reaching the user table, a gap
    # with its reason, an ambiguous endpoint, and a trace before the links stage.
    login = mock["traces"][script.LOGIN]["paths"][0]
    assert login["reaches_goal"]
    assert login["hops"][login["return_index"] - 1]["node"]["name"] == "user"
    # And back up (3.22): the token stored, the page navigated to.
    outcomes = [h["node"]["outcome"] for h in login["hops"][login["return_index"] :]]
    assert {o["kind"] for o in outcomes if o} >= {"storage", "navigate"}
    [gap] = mock["traces"][script.EXPORT]["paths"][0]["breaks"]
    assert gap["detail"]
    endpoint = mock["traces"][script.REPORT]["paths"][0]["hops"][2]["edge"]
    assert endpoint["alternatives"] == 2
    assert mock["unlinked"][script.LOGIN]["linked"] is False
