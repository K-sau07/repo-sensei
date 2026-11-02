"""The CLI against a stand-in server and a keyring in memory (see :mod:`sensei_testing.cli`)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sensei_cli import main
from sensei_testing.cli import SERVER, Cli, FakeServer, MemoryKeyring


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def keys() -> MemoryKeyring:
    return MemoryKeyring()


@pytest.fixture
def clock() -> list[float]:
    return [1_800_000_000.0]


@pytest.fixture(autouse=True)
def runtime(
    server: FakeServer,
    keys: MemoryKeyring,
    clock: list[float],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> main.Runtime:
    monkeypatch.setenv("SENSEI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SENSEI_URL", SERVER)
    monkeypatch.delenv("SENSEI_API_KEY", raising=False)
    monkeypatch.setenv("COLUMNS", "100")

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    found = main.Runtime(
        transport=httpx.MockTransport(server), keyring=keys, sleep=sleep,
        clock=lambda: clock[0], open_browser=lambda _url: None,
    )  # fmt: skip
    monkeypatch.setattr(main, "RUNTIME", found)
    return found


@pytest.fixture
def cli() -> Cli:
    return Cli(Path(__file__).parent / "snapshots")
