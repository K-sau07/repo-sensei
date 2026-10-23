"""Shared pytest fixtures (loaded as a plugin from the repository's root conftest).

Integration fixtures talk to the real local stack started by ``make up`` and use the test
database (``SENSEI_DATABASE_TEST_NAME``). Each test runs inside a transaction that is rolled back
afterwards, so tests never see each other's data.

* ``db_session`` connects as the application role: row-level security is enforced, exactly as in
  production.
* ``owner_session`` connects as the schema owner, for tests that need DDL or cross-org setup.
* ``system_session`` connects as the system role (bypasses row-level security), like workers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sensei_core.config import GitHubSettings, Settings
from sensei_core.models import User


def _find_alembic_ini() -> Path:
    """Locate ``packages/core/alembic.ini`` by walking up from this file."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "packages" / "core" / "alembic.ini"
        if candidate.is_file():
            return candidate
    msg = "could not find packages/core/alembic.ini above the sensei_testing package"
    raise RuntimeError(msg)


ALEMBIC_INI = _find_alembic_ini()
ROOT = ALEMBIC_INI.parents[2]

# Outside services whose credentials tests never take from a developer's .env or shell: a test
# that needs them passes its own, and "not configured" means what it says. Add each new one.
EXTERNAL_SERVICES: tuple[type[BaseSettings], ...] = (GitHubSettings,)


def pytest_configure() -> None:
    for service in EXTERNAL_SERVICES:
        service.model_config["env_file"] = None
        prefix = service.model_config.get("env_prefix", "").upper()
        for key in [k for k in os.environ if k.upper().startswith(prefix)]:
            del os.environ[key]


def settings_for_tests() -> Settings:
    """Settings with every database URL pointing at the test database."""
    settings = Settings(env="test")
    return settings.model_copy(
        update={"database": settings.database.for_database(settings.database.test_name)}
    )


def run_alembic(*args: str, url: str) -> None:
    """Run alembic with the current interpreter; on failure, surface alembic's own output."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "-x", f"url={url}", *args],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        raise RuntimeError(msg)


@pytest.fixture(scope="session")
def migrated_test_db() -> Settings:
    """Bring the test database to the latest schema once per test session."""
    settings = settings_for_tests()
    run_alembic("upgrade", "head", url=settings.database.migrate_url.get_secret_value())
    return settings


@asynccontextmanager
async def rolled_back_session(url: str) -> AsyncGenerator[AsyncSession]:
    """A session inside an outer transaction that is always rolled back on exit."""
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        outer = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as session:
            yield session
        await outer.rollback()
    await engine.dispose()


@pytest.fixture
async def db_session(migrated_test_db: Settings) -> AsyncGenerator[AsyncSession]:
    """App-role session (RLS enforced); everything is rolled back, even explicit commits."""
    async with rolled_back_session(migrated_test_db.database.url.get_secret_value()) as session:
        yield session


@pytest.fixture
async def owner_session(migrated_test_db: Settings) -> AsyncGenerator[AsyncSession]:
    """Schema-owner session for DDL and cross-org fixtures; rolled back like ``db_session``."""
    url = migrated_test_db.database.migrate_url.get_secret_value()
    async with rolled_back_session(url) as session:
        yield session


@pytest.fixture
async def system_session(migrated_test_db: Settings) -> AsyncGenerator[AsyncSession]:
    """System-role session (BYPASSRLS), as workers use for cross-org work; rolled back."""
    url = migrated_test_db.database.system_url.get_secret_value()
    async with rolled_back_session(url) as session:
        yield session


@pytest.fixture
async def fake_redis() -> AsyncGenerator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


_user_counter = iter(range(1, 1_000_000))


async def make_user(db: AsyncSession, **overrides: object) -> User:
    """Insert a user with unique GitHub identity; any column can be overridden."""
    n = next(_user_counter)
    values: dict[str, object] = {
        "github_id": 10_000_000 + n,
        "login": f"user{n}",
        "name": f"Test User {n}",
        "email": f"user{n}@example.com",
    }
    values.update(overrides)
    user = User(**values)
    db.add(user)
    await db.flush()
    return user


@pytest.fixture
def api_app(migrated_test_db: Settings, db_session: AsyncSession) -> FastAPI:
    """The real app, with every request using the rolled-back ``db_session``."""
    from sensei_api.app import create_app  # noqa: PLC0415 - api is only needed by api tests
    from sensei_api.deps import get_db  # noqa: PLC0415

    app = create_app(migrated_test_db)

    async def _test_db() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = _test_db
    return app


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def prod_settings(**overrides: object) -> Settings:
    """Production-mode settings with a throwaway (non-dev) encryption key."""
    import base64  # noqa: PLC0415
    import os  # noqa: PLC0415

    from pydantic import SecretStr  # noqa: PLC0415

    from sensei_core.config import AuthSettings  # noqa: PLC0415

    key = SecretStr(base64.b64encode(os.urandom(32)).decode())
    values: dict[str, object] = {"env": "prod", "auth": AuthSettings(token_encryption_key=key)}
    values.update(overrides)
    return Settings(**values)  # pyright: ignore[reportArgumentType]


async def sign_in(
    app: FastAPI, client: httpx.AsyncClient, db: AsyncSession, user: User | None = None
) -> tuple[User, dict[str, str]]:
    """Create a session for ``user`` (a new one if omitted), install its cookies on ``client`` and
    return headers that satisfy the CSRF checks for unsafe requests."""
    from sensei_api.auth import cookie_names  # noqa: PLC0415
    from sensei_core.auth.sessions import SessionService  # noqa: PLC0415

    user = user or await make_user(db)
    service: SessionService = app.state.session_service
    issued = await service.create(db, user, ip="127.0.0.1", user_agent="pytest")
    session_cookie, csrf_cookie = cookie_names(app.state.settings)
    client.cookies.clear()
    client.cookies.set(session_cookie, issued.token)
    client.cookies.set(csrf_cookie, issued.csrf_token)
    headers = {"Origin": app.state.settings.web.origin, "X-CSRF-Token": issued.csrf_token}
    return user, headers
