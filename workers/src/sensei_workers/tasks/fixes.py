"""A fix's stages (8.4): each one task, which queues the next when it finishes (sandbox stages
on the sandbox queue). See :mod:`sensei_fixer.pipeline`.

The run's row says what runs, not the message: a stage is taken only while the run is queued at
it, so a message delivered twice, sent again by the sweep, or left over from before a retry
does nothing. (No idempotency marker: a stage queued again later, by a retry or an edited plan,
is new work.) The stall sweep (:func:`sweep_stalled_fixes`) fails a stage whose worker
vanished and sends a queued stage's message again when it seems lost.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sensei_core.db.tenancy import set_org_context
from sensei_core.jobs import TaskName, fix_stage_message
from sensei_core.logging import get_logger
from sensei_core.models import FixRun, FixStage
from sensei_core.models.fixes import TERMINAL
from sensei_fixer.budget import Budget
from sensei_fixer.investigate import investigate_stage
from sensei_fixer.pipeline import FixPipeline
from sensei_fixer.reproduce import reproduce_stage
from sensei_fixer.runs import sandbox_runner
from sensei_fixer.stages import StageHandler, default_handlers
from sensei_fixer.sweep import RESEND_EVERY_SECONDS, sweep_stalled
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.fixes_workspace import drop_worktree, fix_workspace
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)


def handlers() -> dict[FixStage, StageHandler]:
    """The stages built so far; the rest stop, saying so."""
    runtime = get_runtime()
    found = default_handlers()
    found[FixStage.INVESTIGATE] = investigate_stage(
        fix_workspace, lambda: runtime.anthropic, runtime.costs
    )
    found[FixStage.REPRODUCE] = reproduce_stage(
        fix_workspace,
        lambda root, setup: sandbox_runner(runtime.settings.sandbox, root, setup),
        lambda: runtime.anthropic,
        runtime.costs,
    )
    return found


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name=TaskName.RUN_FIX_STAGE.value,
    requires=("org_id", "fix_id", "stage"),
)
def run_fix_stage(self: SenseiTask, *, org_id: str, fix_id: str, stage: str) -> str | None:
    """Run the fix's stage; queue the next one (its name), if there is one."""
    runtime = get_runtime()
    pipeline = FixPipeline(
        sessions=runtime.database.session,
        redis=runtime.redis,
        handlers=handlers(),
        budget=Budget.from_settings(runtime.settings.fixer),
    )
    following = runtime.run(
        pipeline.run_stage(uuid.UUID(org_id), uuid.UUID(fix_id), FixStage(stage))
    )
    if following is None:
        if runtime.run(_ended(uuid.UUID(org_id), uuid.UUID(fix_id))):
            drop_worktree(runtime.settings.ingest.cache_dir, uuid.UUID(fix_id))
        return None
    message = fix_stage_message(uuid.UUID(org_id), uuid.UUID(fix_id), following.value)
    self.app.send_task(message["name"], kwargs=message["kwargs"], queue=message["queue"])
    log.info("next fix stage queued", fix_id=fix_id, stage=following.value)
    return following.value


async def _ended(org_id: uuid.UUID, fix_id: uuid.UUID) -> bool:
    """The run is over (its working copy on this machine can go)."""
    async with get_runtime().database.session() as db:
        await set_org_context(db, org_id)
        run = await db.get(FixRun, fix_id)
        return run is None or run.status in TERMINAL


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.SWEEP_STALLED_FIXES.value, system=True)
def sweep_stalled_fixes(self: SenseiTask) -> int:
    """Fail fixes whose stage lost its worker, send lost queued stages again (each at most every
    ``RESEND_EVERY_SECONDS``). Returns how many it failed. See :mod:`sensei_fixer.sweep`."""
    runtime = get_runtime()
    now = datetime.now(UTC)

    async def _sweep() -> tuple[int, list[tuple[uuid.UUID, uuid.UUID, str]]]:
        async with self.system_session() as db:
            swept = await sweep_stalled(
                db, now, time_limit_seconds=runtime.settings.worker.time_limit_seconds
            )
        for outbox in swept.failed:
            await outbox.publish(runtime.redis)
        due: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        for org_id, fix_id, stage in swept.resend:
            key = f"fixes:resent:{fix_id}"
            if await runtime.redis.set(key, stage, nx=True, ex=RESEND_EVERY_SECONDS):
                due.append((org_id, fix_id, stage))
        return len(swept.failed), due

    failed, due = runtime.run(_sweep())
    for org_id, fix_id, stage in due:
        message = fix_stage_message(org_id, fix_id, stage)
        self.app.send_task(message["name"], kwargs=message["kwargs"], queue=message["queue"])
        log.warning("queued fix stage sent again", fix_id=str(fix_id), stage=stage)
    if failed:
        log.warning("stalled fixes failed", fixes=failed)
    return failed
