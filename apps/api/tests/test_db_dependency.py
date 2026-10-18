"""The request-scoped session commits *before* the response is sent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI

from sensei_api.deps import DbSession
from sensei_api.routers import health

API = "/api/v1"


@dataclass
class FakeDatabase:
    """Mimics ``Database.session()``: the commit happens when the context exits."""

    fail_on_commit: bool = False
    events: list[str] = field(default_factory=list[str])

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[object]:
        self.events.append("begin")
        yield object()
        if self.fail_on_commit:
            self.events.append("commit-failed")
            msg = "could not serialize access"
            raise RuntimeError(msg)
        self.events.append("commit")


@pytest.fixture
def write_app(app: FastAPI) -> FastAPI:
    @app.post(f"{API}/_test/write")
    async def write(session: DbSession) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        assert session is not None
        return {"ok": True}

    return app


async def test_successful_request_commits_before_responding(
    write_app: FastAPI, client: httpx.AsyncClient
) -> None:
    database = FakeDatabase()
    write_app.state.database = database
    response = await client.post(f"{API}/_test/write")
    assert response.status_code == 200
    assert database.events == ["begin", "commit"]


async def test_failed_commit_is_reported_not_hidden(
    write_app: FastAPI, client: httpx.AsyncClient
) -> None:
    database = FakeDatabase(fail_on_commit=True)
    write_app.state.database = database
    response = await client.post(f"{API}/_test/write")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert database.events == ["begin", "commit-failed"]


async def test_readyz_without_checks_is_not_ready(app: FastAPI, client: httpx.AsyncClient) -> None:
    app.state.readiness_checks = {}
    response = await client.get(f"{API}/readyz")
    assert response.status_code == 503


async def test_readyz_times_out_hanging_checks(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)

    async def hangs() -> None:
        await asyncio.sleep(10)

    async def ok() -> None:
        return None

    app.state.readiness_checks = {"hangs": hangs, "ok": ok}
    response = await client.get(f"{API}/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"hangs": "error: timeout", "ok": "ok"}
