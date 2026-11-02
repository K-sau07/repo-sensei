"""A fix's stages (8.4): each one task, which queues the next when it finishes (sandbox stages
on the sandbox queue). See :mod:`sensei_fixer.pipeline`."""

from __future__ import annotations

import uuid

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
    idempotent_on=("fix_id", "stage"),
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
    following = runtime.run(pipeline.run_stage(uuid.UUID(org_id), uuid.UUID(fix_id)))
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
