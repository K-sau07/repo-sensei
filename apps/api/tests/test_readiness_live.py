from __future__ import annotations

import httpx
import pytest

from sensei_api.app import create_app
from sensei_core.config import DatabaseSettings, RedisSettings, Settings
from sensei_core.testing import settings_for_tests


async def _readyz(app_settings: Settings) -> httpx.Response:
    app = create_app(app_settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/v1/readyz")


@pytest.mark.integration
async def test_readyz_ok_with_real_dependencies() -> None:
    response = await _readyz(settings_for_tests())
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_readyz_unavailable_when_dependencies_are_down() -> None:
    broken = Settings(
        env="test",
        database=DatabaseSettings(url="postgresql+asyncpg://x:y@127.0.0.1:1/none"),  # pyright: ignore[reportArgumentType]
        redis=RedisSettings(url="redis://127.0.0.1:1/0"),  # pyright: ignore[reportArgumentType]
    )
    response = await _readyz(broken)
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["database"].startswith("error")
    assert checks["redis"].startswith("error")
