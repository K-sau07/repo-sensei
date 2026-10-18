"""Typed application settings.

Every setting comes from environment variables prefixed with ``SENSEI_`` (or a ``.env`` file in
the working directory). Settings are grouped by concern; each group reads a flat prefix such as
``SENSEI_DATABASE_URL`` or ``SENSEI_S3_ENDPOINT`` so the variable names stay short and obvious.

Groups are added here as features need them; ``.env.example`` documents every variable.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

Environment = Literal["dev", "test", "staging", "prod"]


def _config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class DatabaseSettings(BaseSettings):
    """Three roles, three URLs.

    * ``url`` - the application role (``sensei_app``): row-level security is enforced.
    * ``migrate_url`` - the schema owner: runs migrations only.
    * ``system_url`` - ``sensei_system`` (BYPASSRLS): explicitly named maintenance tasks only.
    """

    model_config = _config("SENSEI_DATABASE_")

    url: SecretStr = SecretStr("postgresql+asyncpg://sensei_app:sensei_app@localhost:55432/sensei")
    migrate_url: SecretStr = SecretStr("postgresql+asyncpg://sensei:sensei@localhost:55432/sensei")
    system_url: SecretStr = SecretStr(
        "postgresql+asyncpg://sensei_system:sensei_system@localhost:55432/sensei"
    )
    test_name: str = "sensei_test"
    app_password: SecretStr = SecretStr("sensei_app")
    system_password: SecretStr = SecretStr("sensei_system")
    pool_size: int = Field(default=10, ge=1, le=200)
    max_overflow: int = Field(default=10, ge=0, le=200)
    echo: bool = False

    def for_database(self, name: str) -> DatabaseSettings:
        """The same settings with every URL pointing at database ``name``."""
        return self.model_copy(
            update={
                field: SecretStr(with_database(getattr(self, field).get_secret_value(), name))
                for field in ("url", "migrate_url", "system_url")
            }
        )


def with_database(url: str, name: str) -> str:
    """Replace the database name in a SQLAlchemy URL."""
    return make_url(url).set(database=name).render_as_string(hide_password=False)


class RedisSettings(BaseSettings):
    model_config = _config("SENSEI_REDIS_")

    url: SecretStr = SecretStr("redis://localhost:56379/0")


class S3Settings(BaseSettings):
    model_config = _config("SENSEI_S3_")

    endpoint: str | None = "http://localhost:9000"
    access_key: SecretStr = SecretStr("sensei")
    secret_key: SecretStr = SecretStr("sensei-dev-secret")
    region: str = "us-east-1"
    bucket_snapshots: str = "sensei-snapshots"
    bucket_artifacts: str = "sensei-artifacts"


class WebSettings(BaseSettings):
    model_config = _config("SENSEI_WEB_")

    origin: str = "http://localhost:3000"


class TelemetrySettings(BaseSettings):
    model_config = _config("SENSEI_")

    otel_endpoint: str | None = None  # OTLP/HTTP base URL, e.g. http://localhost:4318
    otel_console: bool = False  # print spans to stderr (local debugging)
    sentry_dsn: SecretStr | None = None
    traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class Settings(BaseSettings):
    """Root settings object. Use :func:`get_settings` instead of constructing directly."""

    model_config = _config("SENSEI_")

    env: Environment = "dev"
    service_name: str = "repo-sensei"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool | None = None  # None means: JSON everywhere except dev

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    s3: S3Settings = Field(default_factory=S3Settings)
    web: WebSettings = Field(default_factory=WebSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def use_json_logs(self) -> bool:
        return self.log_json if self.log_json is not None else not self.is_dev


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached). Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
