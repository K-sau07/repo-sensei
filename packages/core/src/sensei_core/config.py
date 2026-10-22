"""Typed application settings.

Every setting comes from environment variables prefixed with ``SENSEI_`` (or a ``.env`` file in
the working directory). Settings are grouped by concern; each group reads a flat prefix such as
``SENSEI_DATABASE_URL`` or ``SENSEI_S3_ENDPOINT`` so the variable names stay short and obvious.

Groups are added here as features need them; ``.env.example`` documents every variable.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

Environment = Literal["dev", "test", "staging", "prod"]


def _config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `KEY=` in .env means "not set", not "empty string"
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
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
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
    connect_timeout_seconds: float = Field(default=2.0, gt=0)
    timeout_seconds: float = Field(default=5.0, gt=0)


def with_redis_db(url: str, db: int) -> str:
    """The same Redis URL pointing at logical database ``db``."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{db}"))


class WorkerSettings(BaseSettings):
    """Celery. Broker and results default to databases 1 and 2 of the main Redis."""

    model_config = _config("SENSEI_WORKER_")

    broker_url: SecretStr | None = None
    result_backend: SecretStr | None = None
    soft_time_limit_seconds: int = Field(default=1800, gt=0)
    time_limit_seconds: int = Field(default=2100, gt=0)
    # Redis hands an unacknowledged message to another worker this long after it was received,
    # however long it then waits (a retry countdown) or runs. So a countdown plus the longest run
    # must fit inside it: see max_countdown_seconds.
    visibility_timeout_seconds: int = Field(default=3600, gt=0)
    result_expires_seconds: int = Field(default=86_400, gt=0)
    max_retries: int = Field(default=5, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, gt=0)
    retry_backoff_max_seconds: float = Field(default=600.0, gt=0)
    # A running idempotent task refreshes its lock this often; a copy of the same message that
    # finds a fresh heartbeat waits instead of running alongside it.
    lock_heartbeat_seconds: float = Field(default=30.0, gt=0)
    # The same attempt delivered this many times (its process died each time) is dead-lettered
    # instead of crashing workers forever.
    max_deliveries: int = Field(default=3, ge=1)
    # Tasks run in the caller, synchronously (tests).
    always_eager: bool = False

    @property
    def max_countdown_seconds(self) -> float:
        """The longest retry delay that can't make Redis redeliver a task that is still running."""
        return self.visibility_timeout_seconds - self.time_limit_seconds - 60

    @model_validator(mode="after")
    def _limits_are_ordered(self) -> WorkerSettings:
        limits = (self.soft_time_limit_seconds, self.time_limit_seconds)
        if not limits[0] < limits[1] < self.visibility_timeout_seconds:
            msg = "worker limits must satisfy soft time limit < time limit < visibility timeout"
            raise ValueError(msg)
        if self.max_countdown_seconds < 60:
            msg = "the visibility timeout must exceed the time limit by at least two minutes"
            raise ValueError(msg)
        return self


class IngestSettings(BaseSettings):
    """Cloning repositories into the workers' cache."""

    model_config = _config("SENSEI_INGEST_")

    # One working copy per repository, reused between index runs (the image sets /data/repos).
    cache_dir: Path = Path.home() / ".cache" / "repo-sensei" / "repos"
    cache_max_bytes: int = Field(default=50 * 1024**3, gt=0)
    # Checked-out size limit; bigger repositories are refused before checkout.
    max_checkout_bytes: int = Field(default=2 * 1024**3, gt=0)
    clone_timeout_seconds: float = Field(default=900.0, gt=0)
    # Files kept after the ignore rules; a bigger tree fails discovery.
    max_files: int = Field(default=250_000, gt=0)
    # Where the API unpacks snapshots to serve files from (it has no working copies), and the
    # disk budget for them.
    snapshot_cache_dir: Path = Path.home() / ".cache" / "repo-sensei" / "snapshots"
    snapshot_cache_max_bytes: int = Field(default=20 * 1024**3, gt=0)
    git_binary: str = "git"
    # file:// remotes (local test fixtures only; never in a deployment).
    allow_file_protocol: bool = False


class S3Settings(BaseSettings):
    model_config = _config("SENSEI_S3_")

    endpoint: str | None = "http://localhost:9000"
    access_key: SecretStr = SecretStr("sensei")
    secret_key: SecretStr = SecretStr("sensei-dev-secret")
    region: str = "us-east-1"
    bucket_snapshots: str = "sensei-snapshots"
    bucket_artifacts: str = "sensei-artifacts"


class ApiSettings(BaseSettings):
    model_config = _config("SENSEI_API_")

    # The header the platform's proxy sets to the client's address, overwriting whatever the
    # client sent (Fly.io: "fly-client-ip"). None: the connecting peer's address. Never trust a
    # header a client can forge, such as X-Forwarded-For's first entry.
    client_ip_header: str | None = None
    # Memory for what browsing keeps decoded (per API process), sized for a 512 MB machine:
    # Merkle trees (about 700 bytes a file) and constellations (about 650 bytes a point),
    # least recently used out first. One snapshot larger than its budget is still served.
    tree_cache_mb: int = Field(default=96, gt=0)
    constellation_cache_mb: int = Field(default=64, gt=0)


class WebSettings(BaseSettings):
    model_config = _config("SENSEI_WEB_")

    origin: str = "http://localhost:3000"


# 32 readable bytes ("repo-sensei-dev-key-not-a-secret"): usable only in dev and test.
DEV_TOKEN_ENCRYPTION_KEY = "cmVwby1zZW5zZWktZGV2LWtleS1ub3QtYS1zZWNyZXQ="  # noqa: S105 - public dev key


class AuthSettings(BaseSettings):
    model_config = _config("SENSEI_AUTH_")

    # base64 of 32 random bytes. Rotate by moving the old key to *_previous and setting a new one.
    token_encryption_key: SecretStr = SecretStr(DEV_TOKEN_ENCRYPTION_KEY)
    token_encryption_key_previous: SecretStr | None = None
    session_idle_days: int = Field(default=14, ge=1, le=90)
    session_absolute_days: int = Field(default=30, ge=1, le=365)
    session_touch_after_seconds: int = Field(default=3600, ge=60)
    cookie_secure: bool | None = None  # None means: secure everywhere except dev


class GitHubSettings(BaseSettings):
    """A GitHub App provides both sign-in (user authorization) and repository access."""

    model_config = _config("SENSEI_GITHUB_")

    app_id: int | None = None
    app_slug: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    private_key_path: str | None = None
    # The PEM itself, for platforms where secrets are env vars rather than files.
    private_key: SecretStr | None = None
    webhook_secret: SecretStr | None = None
    api_url: str = "https://api.github.com"
    web_url: str = "https://github.com"
    timeout_seconds: float = Field(default=10.0, gt=0)
    # Wait this long at most for a rate limit to reset before giving up with an error.
    rate_limit_max_wait_seconds: float = Field(default=30.0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    # GitHub caps webhook payloads at 25 MB; anything bigger is not from GitHub.
    webhook_max_bytes: int = Field(default=25 * 1024 * 1024, gt=0)

    @property
    def oauth_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def app_configured(self) -> bool:
        """Installing the app and acting as it need its slug, an issuer id and the private key."""
        return bool(
            self.app_slug
            and (self.client_id or self.app_id)
            and (self.private_key or self.private_key_path)
        )

    def private_key_pem(self) -> str:
        if self.private_key is not None:
            return self.private_key.get_secret_value()
        if self.private_key_path:
            return Path(self.private_key_path).expanduser().read_text()
        msg = "no GitHub App private key configured"
        raise RuntimeError(msg)


class TelemetrySettings(BaseSettings):
    model_config = _config("SENSEI_")

    otel_endpoint: str | None = None  # OTLP/HTTP base URL, e.g. http://localhost:4318
    otel_console: bool = False  # print spans to stderr (local debugging)
    sentry_dsn: SecretStr | None = None
    traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class PricingSettings(BaseSettings):
    model_config = _config("SENSEI_PRICING_")

    # A JSON price table (see sensei_core.usage.PriceTable); the built-in table when unset.
    table_path: Path | None = None


class Settings(BaseSettings):
    """Root settings object. Use :func:`get_settings` instead of constructing directly."""

    model_config = _config("SENSEI_")

    # Safe by default: a deployment that forgets SENSEI_ENV runs with production behaviour.
    # Local development opts in with SENSEI_ENV=dev (see .env.example).
    env: Environment = "prod"
    service_name: str = "repo-sensei"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool | None = None  # None means: JSON everywhere except dev

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    s3: S3Settings = Field(default_factory=S3Settings)
    web: WebSettings = Field(default_factory=WebSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)

    @model_validator(mode="after")
    def _refuse_dev_secrets_outside_dev(self) -> Settings:
        if self.env in ("staging", "prod"):
            key = self.auth.token_encryption_key.get_secret_value()
            if key == DEV_TOKEN_ENCRYPTION_KEY:
                msg = "SENSEI_AUTH_TOKEN_ENCRYPTION_KEY must be set to a real key outside dev/test"
                raise ValueError(msg)
        return self

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def use_json_logs(self) -> bool:
        return self.log_json if self.log_json is not None else not self.is_dev

    @property
    def secure_cookies(self) -> bool:
        return self.auth.cookie_secure if self.auth.cookie_secure is not None else not self.is_dev


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached). Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
