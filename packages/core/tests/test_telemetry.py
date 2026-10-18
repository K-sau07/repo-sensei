from __future__ import annotations

import pytest
import sentry_sdk
from pydantic import SecretStr

from sensei_core import telemetry
from sensei_core.config import Settings, TelemetrySettings

GH_TOKEN = "ghs_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"


@pytest.fixture
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(telemetry._state, "tracing", False)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setitem(telemetry._state, "sentry", False)  # pyright: ignore[reportPrivateUsage]


def test_tracing_is_noop_without_exporters(fresh_state: None) -> None:
    assert telemetry.init_tracing(Settings(env="test"), "svc") is False


def test_sentry_is_noop_without_dsn(fresh_state: None) -> None:
    assert telemetry.init_sentry(Settings(env="test"), "svc") is False


def test_sentry_init_is_idempotent(fresh_state: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    settings = Settings(
        env="prod",
        telemetry=TelemetrySettings(sentry_dsn=SecretStr("https://k@sentry.example.com/1")),
    )
    assert telemetry.init_sentry(settings, "svc") is True
    assert telemetry.init_sentry(settings, "svc") is False
    assert len(calls) == 1
    assert calls[0]["send_default_pii"] is False
    assert calls[0]["before_send"] is telemetry.sentry_before_send


def test_sentry_events_are_redacted() -> None:
    event = {
        "message": f"clone failed with {GH_TOKEN}",
        "request": {"headers": {"Authorization": "Bearer abcdefghijklmnop", "accept": "json"}},
        "extra": {"password": "hunter2", "files": 12},
    }
    cleaned = telemetry.sentry_before_send(event, None)
    assert GH_TOKEN not in str(cleaned)
    assert "hunter2" not in str(cleaned)
    assert cleaned["request"]["headers"]["accept"] == "json"
    assert cleaned["extra"]["files"] == 12
