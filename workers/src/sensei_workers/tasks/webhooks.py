"""Processing stored GitHub webhook deliveries (system tasks: deliveries are not org scoped)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sensei_core.github.webhooks import ProcessResult, claim_stale_deliveries, process_delivery
from sensei_core.jobs import CeleryPushTimers, TaskName
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import get_runtime

# A delivery queued this long ago and still waiting is queued again (the queue may have been down).
STALE_AFTER = timedelta(minutes=10)
SWEEP_LIMIT = 500


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name=TaskName.PROCESS_GITHUB_WEBHOOK.value,
    system=True,
    requires=("delivery_id",),
    idempotent_on=("delivery_id",),
)
def process_github_webhook(self: SenseiTask, *, delivery_id: str) -> str | None:
    runtime = get_runtime()

    async def _process() -> ProcessResult:
        async with self.system_session() as db:
            return await process_delivery(
                db,
                runtime.redis,
                uuid.UUID(delivery_id),
                app=runtime.github_app,
                timers=CeleryPushTimers(celery_app),
            )

    # The delivery's status (failed, with the error) is committed before deciding on a retry.
    result = runtime.run(_process())
    if result.error is not None:
        raise result.error
    return result.status.value if result.status else None


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.SWEEP_WEBHOOKS.value, system=True)
def sweep_webhooks(self: SenseiTask) -> int:
    """Queue deliveries that were stored but never picked up."""

    async def _stale() -> list[uuid.UUID]:
        async with self.system_session() as db:
            return await claim_stale_deliveries(db, older_than=STALE_AFTER, limit=SWEEP_LIMIT)

    stale = get_runtime().run(_stale())
    for delivery_id in stale:
        process_github_webhook.apply_async(kwargs={"delivery_id": str(delivery_id)})
    return len(stale)
