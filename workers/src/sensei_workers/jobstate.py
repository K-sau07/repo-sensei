"""Changing an index job from a task: lock the row, move the state machine, publish the events.

::

    async with job_scope(task, org_id, job_id) as scope:
        scope.require(Stage.DISCOVER)          # stop quietly if it was cancelled meanwhile
        scope.machine.update_progress(Stage.DISCOVER, 500, 2000)

The events the machine records are collected before the transaction commits and published to
the job's stream once it has (see :mod:`sensei_core.indexing.events`); a rolled-back change is
never announced.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.indexing.events import JobOutbox
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.models import IndexJob, JobStatus, Stage
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.runtime import get_runtime


class JobGoneError(NotYetError):
    """The job was cancelled, failed or deleted meanwhile: stop without doing anything more."""


@dataclass(slots=True)
class JobScope:
    db: AsyncSession
    job: IndexJob | None
    outbox: JobOutbox = field(default_factory=JobOutbox)
    _machine: IndexJobStateMachine | None = None

    @property
    def machine(self) -> IndexJobStateMachine:
        if self.job is None:
            raise JobGoneError("the job is gone")
        if self._machine is None:
            self._machine = IndexJobStateMachine(self.job)
        return self._machine

    def collect(self) -> None:
        if self._machine is not None:
            self.outbox.collect(self._machine)

    def require(self, stage: Stage) -> IndexJob:
        """The job, if it is still running ``stage``; otherwise stop the task quietly."""
        job = self.job
        running = job is not None and job.status is JobStatus.RUNNING and job.stage is stage
        if not running or job is None:
            raise JobGoneError(f"the job is no longer at {stage.value}")
        return job


@asynccontextmanager
async def job_scope(
    task: SenseiTask, org_id: uuid.UUID | str, job_id: uuid.UUID
) -> AsyncGenerator[JobScope]:
    """The job row locked (``FOR UPDATE``) in an org-scoped transaction."""
    async with task.org_session(org_id) as db:
        job = await db.scalar(select(IndexJob).where(IndexJob.id == job_id).with_for_update())
        scope = JobScope(db, job)
        yield scope
        scope.collect()  # before the commit, while the job row is current
    await scope.outbox.publish(get_runtime().redis)
