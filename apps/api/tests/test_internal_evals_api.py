"""Staff routes over stored eval runs.

``eval_runs`` is written only by the system role, so these tests commit their runs through it
(the app-role request session can't see another connection's uncommitted rows) and delete them
afterwards. Each test uses its own suite name, so other rows in the table don't matter."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import Settings
from sensei_core.db import make_system_database
from sensei_core.models import EvalRun
from sensei_testing import make_user, sign_in

pytestmark = pytest.mark.integration

API = "/api/v1"
SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e"


def _run(suite: str, retriever: str, recall: float) -> EvalRun:
    return EvalRun(
        suite=suite,
        suite_version=0,
        retriever=retriever,
        config={"repo": "fastapi-template", "ks": [10]},
        repo_sha=SHA,
        metrics={"overall": {"recall_symbol@10": recall}},
        per_item=[{"id": "item-1", "metrics": {"recall_symbol@10": recall}}],
        created_at=datetime.now(UTC),
        git_sha="1" * 40,
    )


@pytest.fixture
async def runs(migrated_test_db: Settings) -> AsyncGenerator[tuple[str, list[EvalRun]]]:
    suite = f"api-{uuid.uuid4().hex[:8]}"
    rows = [_run(suite, name, recall) for name, recall in [("random", 0.1), ("oracle", 1.0)]]
    rows.append(_run(suite, "random", 0.2))
    database = make_system_database(migrated_test_db.database)
    try:
        async with database.session() as db:
            db.add_all(rows)  # ids are uuid7: in this order, oldest first
        yield suite, rows
    finally:
        async with database.session() as db:
            await db.execute(delete(EvalRun).where(EvalRun.suite == suite))
        await database.dispose()


async def _staff(api_app: FastAPI, client: httpx.AsyncClient, db: AsyncSession) -> None:
    await sign_in(api_app, client, db, await make_user(db, is_staff=True))


async def test_staff_page_through_runs_newest_first(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    runs: tuple[str, list[EvalRun]],
) -> None:
    suite, rows = runs
    await _staff(api_app, api_client, db_session)
    first = await api_client.get(f"{API}/internal/evals", params={"suite": suite, "limit": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert [item["id"] for item in body["items"]] == [str(rows[2].id), str(rows[1].id)]
    assert body["items"][0]["metrics"] == {"overall": {"recall_symbol@10": 0.2}}
    assert "per_item" not in body["items"][0]
    assert body["next_cursor"] == str(rows[1].id)

    rest = await api_client.get(
        f"{API}/internal/evals", params={"suite": suite, "before": body["next_cursor"]}
    )
    assert [item["id"] for item in rest.json()["items"]] == [str(rows[0].id)]
    assert rest.json()["next_cursor"] is None

    only_random = await api_client.get(
        f"{API}/internal/evals", params={"suite": suite, "retriever": "random"}
    )
    assert [item["retriever"] for item in only_random.json()["items"]] == ["random", "random"]


async def test_staff_read_one_run_with_its_items(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    runs: tuple[str, list[EvalRun]],
) -> None:
    _, rows = runs
    await _staff(api_app, api_client, db_session)
    response = await api_client.get(f"{API}/internal/evals/{rows[1].id}")
    assert response.status_code == 200
    body = response.json()
    assert (body["retriever"], body["repo_sha"], body["git_sha"]) == ("oracle", SHA, "1" * 40)
    assert body["per_item"] == [{"id": "item-1", "metrics": {"recall_symbol@10": 1.0}}]

    missing = await api_client.get(f"{API}/internal/evals/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_everyone_else_sees_nothing(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    runs: tuple[str, list[EvalRun]],
) -> None:
    _, rows = runs
    anonymous = await api_client.get(f"{API}/internal/evals")
    assert anonymous.status_code == 401

    await sign_in(api_app, api_client, db_session)  # an ordinary user
    for path in ("/internal/evals", f"/internal/evals/{rows[0].id}"):
        response = await api_client.get(f"{API}{path}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
