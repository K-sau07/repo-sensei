from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from sensei_core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # Run from an empty directory so a developer's .env never leaks into tests.
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_point_at_local_dev_stack() -> None:
    settings = Settings()
    assert settings.env == "dev"
    assert settings.database.url.get_secret_value().endswith("@localhost:55432/sensei")
    assert settings.redis.url.get_secret_value() == "redis://localhost:56379/0"
    assert settings.s3.bucket_snapshots == "sensei-snapshots"


def test_reads_flat_prefixed_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSEI_ENV", "staging")
    monkeypatch.setenv("SENSEI_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("SENSEI_DATABASE_POOL_SIZE", "25")
    monkeypatch.setenv("SENSEI_S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("SENSEI_WEB_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("SENSEI_SENTRY_DSN", "https://key@sentry.example.com/1")

    settings = Settings()

    assert settings.env == "staging"
    assert settings.database.url.get_secret_value() == "postgresql+asyncpg://u:p@db:5432/x"
    assert settings.database.pool_size == 25
    assert settings.s3.endpoint == "https://s3.example.com"
    assert settings.web.origin == "https://app.example.com"
    assert settings.telemetry.sentry_dsn is not None


def test_reads_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("SENSEI_ENV=test\nSENSEI_REDIS_URL=redis://cache:6379/3\n")
    settings = Settings()
    assert settings.env == "test"
    assert settings.redis.url.get_secret_value() == "redis://cache:6379/3"


def test_secrets_are_hidden_in_repr() -> None:
    settings = Settings()
    assert "sensei-dev-secret" not in repr(settings)
    assert "sensei-dev-secret" not in str(settings.model_dump())


def test_json_logs_default_depends_on_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().use_json_logs is False
    monkeypatch.setenv("SENSEI_ENV", "prod")
    assert Settings().use_json_logs is True
    monkeypatch.setenv("SENSEI_LOG_JSON", "false")
    assert Settings().use_json_logs is False


def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSEI_ENV", "production-ish")
    with pytest.raises(ValueError, match="env"):
        Settings()


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
