"""Fixes nothing is moving any more (8.4), found by the periodic stall sweep.

* A stage ``running`` for longer than any task may run (the worker's hard time limit, plus a
  margin) lost its worker (a restart, a deploy, the time limit, a database blip as it
  finished): the fix fails, saying so. Its worker, were it somehow still there, finds the run
  failed and drops what it found.
* A stage ``queued`` for a while may have lost its message (the broker was unreachable): it is
  sent again (a duplicate does nothing: the run's row decides what runs). One queued for over
  ``give_up_after`` fails: no worker for it.

Runs a stage holds locked are skipped (it is finishing them).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import FixRun, FixStatus
from sensei_fixer.events import FixOutbox
from sensei_fixer.pipeline import close_agent_run
from sensei_fixer.state import FixStateMachine

STALL_MARGIN_SECONDS = 600
REQUEUE_AFTER_SECONDS = 600
RESEND_EVERY_SECONDS = 1800
GIVE_UP_AFTER_SECONDS = 24 * 3600


@dataclass
class Swept:
    failed: list[FixOutbox] = field(default_factory=list[FixOutbox])  # their events, to publish
    resend: list[tuple[uuid.UUID, uuid.UUID, str]] = field(
        default_factory=list[tuple[uuid.UUID, uuid.UUID, str]]
    )  # (org, fix, stage): queued a while, sent again


async def sweep_stalled(
    db: AsyncSession,
    now: datetime,
    *,
    time_limit_seconds: float,
    requeue_after: float = REQUEUE_AFTER_SECONDS,
    give_up_after: float = GIVE_UP_AFTER_SECONDS,
) -> Swept:
    """Fail the stalled runs ``db`` (a system session: every org's) can see, and name the
    queued ones to send again. The caller commits, then publishes and sends."""
    swept = Swept()
    runs = await db.scalars(
        select(FixRun)
        .where(FixRun.status.in_((FixStatus.QUEUED, FixStatus.RUNNING)))
        .order_by(FixRun.status_at)
        .with_for_update(skip_locked=True)
    )
    for run in runs:
        age = (now - run.status_at).total_seconds()
        stage = run.stage.value
        machine = FixStateMachine(run, lambda: now)
        if run.status is FixStatus.RUNNING:
            if age <= time_limit_seconds + STALL_MARGIN_SECONDS:
                continue
            machine.stop(
                FixStatus.FAILED,
                f"The {stage} stage's worker stopped before it finished (it was restarted, or "
                "ran out of time).",
            )
        else:
            if age < requeue_after:
                continue
            if age < give_up_after:
                swept.resend.append((run.org_id, run.id, stage))
                continue
            hours = give_up_after / 3600
            machine.stop(FixStatus.FAILED, f"No worker took the {stage} stage in {hours:g} hours.")
        await close_agent_run(db, run, lambda: now)
        outbox = FixOutbox(run.org_id, run.id, run.repo_id)
        outbox.add(machine.events)
        swept.failed.append(outbox)
    return swept
