"""A branch's push timer (5.1): once its pushes stop, the run that indexes them.

The webhook handler notes each push and sets this task for :data:`~sensei_core.indexing.pushes.
DEBOUNCE_SECONDS` later. Only the latest push's timer goes on (see :mod:`sensei_core.indexing.
pushes`): it starts the run (incremental, or full after a forced push), and if the repository
has one going it waits, trying again every :data:`~sensei_core.indexing.pushes.WAIT_SECONDS`.
"""

from __future__ import annotations

import enum
import uuid

from redis.asyncio import Redis

from sensei_core.indexing import pushes
from sensei_core.indexing.events import JobOutbox
from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import IndexJob, Repo
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)


class Outcome(enum.StrEnum):
    STARTED = "started"
    WAITING = "waiting"  # a run is going: tried again later
    INDEXED = "indexed"  # its commit was indexed already: the ref points at it, nothing ran
    SUPERSEDED = "superseded"  # a later push's timer has the branch (or it was indexed)
    UNTRACKED = "untracked"  # the repository went, or no longer tracks the branch


async def begin(
    task: SenseiTask,
    redis: Redis,
    *,
    org_id: uuid.UUID,
    repo_id: uuid.UUID,
    branch: str,
    token: str,
) -> tuple[Outcome, IndexJob | None]:
    """What the timer ``token`` does now: start the branch's run (committed, its event out), or
    say why not."""
    pending = await pushes.due(redis, repo_id, branch, token)
    if pending is None:
        return Outcome.SUPERSEDED, None
    outbox = JobOutbox()
    outcome = Outcome.UNTRACKED
    async with task.org_session(org_id) as db:
        repo = await db.get(Repo, repo_id)
        if repo is None or not pushes.indexable(repo) or not pushes.tracks(repo, branch):
            job = None
        elif await pushes.running(db, repo):
            return Outcome.WAITING, None
        else:
            try:
                job = await pushes.start(db, repo, branch, pending, outbox)
            except pushes.JobRunningError:
                return Outcome.WAITING, None
            if job is None:
                outcome = Outcome.INDEXED
    await pushes.done(redis, repo_id, branch, token)
    if job is None:
        return outcome, None
    await outbox.publish(redis)
    return Outcome.STARTED, job


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name=TaskName.START_PUSH_INDEX.value,
    requires=("org_id", "repo_id", "branch", "token"),
)
def start_push_index(
    self: SenseiTask, *, org_id: str, repo_id: str, branch: str, token: str
) -> str:
    runtime = get_runtime()
    outcome, job = runtime.run(
        begin(
            self,
            runtime.redis,
            org_id=uuid.UUID(org_id),
            repo_id=uuid.UUID(repo_id),
            branch=branch,
            token=token,
        )
    )
    kwargs = {"org_id": org_id, "repo_id": repo_id, "branch": branch, "token": token}
    if outcome is Outcome.WAITING:
        start_push_index.apply_async(kwargs=kwargs, countdown=pushes.WAIT_SECONDS)
    elif job is not None:
        from sensei_workers.tasks.orchestrate import run_index_job  # noqa: PLC0415 - its chain

        run_index_job.apply_async(
            kwargs={"job_id": str(job.id), "org_id": org_id, "repo_id": repo_id}
        )
    log.info("push timer", outcome=outcome.value, branch=branch)
    return outcome.value
