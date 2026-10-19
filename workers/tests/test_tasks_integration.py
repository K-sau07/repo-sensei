"""Real tasks against the test database: webhook processing and dead letters.

Tasks commit (they own their transactions), so each test removes what it wrote. Tests are
synchronous like a worker; database helpers run on the worker runtime's event loop.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import delete, select

from sensei_core.config import Settings
from sensei_core.db import Database
from sensei_core.github.webhooks import record_delivery
from sensei_core.models import DeliveryStatus, JobFailure, WebhookDelivery
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import Runtime, set_runtime
from sensei_workers.tasks.webhooks import process_github_webhook, sweep_webhooks

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime(migrated_test_db: Settings) -> Iterator[Runtime]:
    current = Runtime(migrated_test_db)
    set_runtime(current)
    yield current
    set_runtime(None)


@pytest.fixture
def system_db(runtime: Runtime) -> Database:
    return runtime.system_database


async def _store(system_db: Database, event: str, payload: dict[str, Any]) -> uuid.UUID:
    async with system_db.session() as db:
        stored = await record_delivery(
            db, delivery_id=f"test-{uuid.uuid4()}", event=event, payload=payload
        )
    assert stored is not None
    return stored


async def _delivery(system_db: Database, delivery_id: uuid.UUID) -> WebhookDelivery:
    async with system_db.session() as db:
        row = await db.get(WebhookDelivery, delivery_id)
    assert row is not None
    return row


async def _cleanup(system_db: Database, *ids: uuid.UUID) -> None:
    async with system_db.session() as db:
        await db.execute(delete(WebhookDelivery).where(WebhookDelivery.id.in_(ids)))


def test_webhook_task_processes_a_delivery(runtime: Runtime, system_db: Database) -> None:
    stored = runtime.run(_store(system_db, "ping", {"zen": "Keep it logically awesome."}))
    try:
        result = process_github_webhook.apply(kwargs={"delivery_id": str(stored)})
        assert result.get() == "ignored"
        row = runtime.run(_delivery(system_db, stored))
        assert (row.status, row.attempts) == (DeliveryStatus.IGNORED, 1)
        # A second run of the same delivery is skipped by the idempotency marker.
        assert process_github_webhook.apply(kwargs={"delivery_id": str(stored)}).get() is None
    finally:
        runtime.run(_cleanup(system_db, stored))


async def _age(system_db: Database, delivery_id: uuid.UUID) -> None:
    async with system_db.session() as db:
        row = await db.get(WebhookDelivery, delivery_id)
        assert row is not None
        row.received_at = row.received_at.replace(year=row.received_at.year - 1)


def test_sweep_requeues_deliveries_that_were_never_dispatched(
    runtime: Runtime, system_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = runtime.run(_store(system_db, "push", {}))
    stale = runtime.run(_store(system_db, "push", {}))
    runtime.run(_age(system_db, stale))
    queued: list[dict[str, Any]] = []

    def capture(*_: Any, **options: Any) -> None:
        queued.append(options["kwargs"])

    monkeypatch.setattr(process_github_webhook, "apply_async", capture)
    try:
        assert sweep_webhooks.apply().get() >= 1
        assert {"delivery_id": str(stale)} in queued
        assert {"delivery_id": str(fresh)} not in queued
    finally:
        runtime.run(_cleanup(system_db, fresh, stale))


@celery_app.task(bind=True, base=SenseiTask, name="sensei.default.test_always_broken")
def always_broken(self: SenseiTask, *, org_id: str | None = None, token: str) -> None:
    msg = f"cannot handle {token[:4]}"
    raise ValueError(msg)


async def _failure(system_db: Database, task_id: str) -> JobFailure | None:
    async with system_db.session() as db:
        return await db.scalar(select(JobFailure).where(JobFailure.task_id == task_id))


async def _forget_failure(system_db: Database, task_id: str) -> None:
    async with system_db.session() as db:
        await db.execute(delete(JobFailure).where(JobFailure.task_id == task_id))


def test_final_failures_are_dead_lettered(runtime: Runtime, system_db: Database) -> None:
    result = always_broken.apply(kwargs={"token": "ghs_supersecretvalue1234567890"})
    assert result.state == "FAILURE"
    try:
        row = runtime.run(_failure(system_db, result.id))
        assert row is not None
        assert (row.task, row.error_type, row.error, row.attempts) == (
            "sensei.default.test_always_broken",
            "ValueError",
            "cannot handle ghs_",
            1,
        )
        assert row.args["kwargs"]["token"] == "***"  # arguments are redacted
        assert row.traceback is not None
        assert "ValueError" in row.traceback
    finally:
        runtime.run(_forget_failure(system_db, result.id))
