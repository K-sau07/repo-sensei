"""The global spend cap (10.1): every org's spend today, every minute (beat); past the cap the
service is read-only until midnight UTC. See :mod:`sensei_core.limits`."""

from __future__ import annotations

from sensei_core.jobs import TaskName
from sensei_core.limits import check_global_spend
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import get_runtime


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.CHECK_GLOBAL_SPEND.value, system=True)
def check_spend(self: SenseiTask) -> bool:
    """Whether the service is read-only now (the system database: every org's spend)."""
    runtime = get_runtime()
    cap = runtime.settings.limits.global_daily_spend_cap_usd

    async def check() -> bool:
        async with self.system_session() as db:
            return await check_global_spend(db, runtime.redis, cap)

    return runtime.run(check())
