"""Running an index job: its stages as a Celery chain, and the stage that finishes it.

``run_index_job`` turns a queued job into the chain clone → discover → merkle → finalize (later
phases add parse, symbols, chunks, embeddings, summaries, graph and links in between). Each stage
is its own task, so a failure retries only that stage. Every stage checks the job first: a
cancelled or failed job makes the rest of the chain do nothing. The stage task ids are kept in
Redis so cancelling can revoke the ones still queued.

``finalize_index`` ends the run: stages this phase doesn't have yet are recorded as skipped, the
repository becomes ``ready`` at the indexed commit, and the job succeeds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import chain
from sqlalchemy import select

from sensei_core.indexing.events import JobOutbox
from sensei_core.indexing.jobs import settle_repo
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.jobs import TaskName, chain_key
from sensei_core.logging import get_logger
from sensei_core.models import ACTIVE_JOB_STATUSES, IndexJob, JobStatus, Repo, Snapshot, Stage
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.jobstate import job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.tasks.discover import discover_files
from sensei_workers.tasks.ingest import IngestTask, clone_snapshot
from sensei_workers.tasks.merkle import build_merkle

log = get_logger(__name__)

CHAIN_TTL_SECONDS = 2 * 86_400
# A running stage with no progress for the task time limit plus this is dead.
STALL_MARGIN_SECONDS = 600
# A job still queued this long after it was created never got its message.
REQUEUE_AFTER_SECONDS = 600


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.FINALIZE_INDEX.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def finalize_index(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> str | None:
    """Finish the job: the repository is ready at the snapshot's commit. Returns the commit."""

    async def _finalize() -> str | None:
        async with job_scope(self, org_id, uuid.UUID(job_id)) as scope:
            job = scope.job
            if job is None or job.status not in ACTIVE_JOB_STATUSES:
                return None  # finished, failed or cancelled meanwhile
            stages = job.stages or {}
            merkle_done = (
                job.stage is Stage.MERKLE and stages.get("merkle", {}).get("status") == "done"
            )
            resuming = job.stage is Stage.FINALIZE
            if not merkle_done and not resuming:
                if job.stage in (None, Stage.CLONE, Stage.DISCOVER, Stage.MERKLE):
                    raise NotYetError("the merkle stage hasn't finished")
                return None
            repo = await scope.db.get(Repo, job.repo_id)
            snapshot = await scope.db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
            machine = scope.machine
            if repo is None or repo.deleted_at is not None:
                machine.cancel()
                return None
            if snapshot is None:
                raise NotYetError("the job has no snapshot")
            if merkle_done:
                machine.advance(Stage.FINALIZE)  # later phases' stages: skipped for now
            repo.last_indexed_sha = snapshot.commit_sha
            settle_repo(repo, JobStatus.SUCCEEDED)
            machine.log(
                f"Indexed {snapshot.commit_sha[:12]}: {snapshot.file_count or 0:,} files. "
                "The code browser is ready."
            )
            machine.finish()
            return snapshot.commit_sha

    sha = get_runtime().run(_finalize())
    if sha is not None:
        log.info("index job succeeded", commit_sha=sha)
    return sha


STAGES = (clone_snapshot, discover_files, build_merkle, finalize_index)


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name=TaskName.RUN_INDEX_JOB.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def run_index_job(self: SenseiTask, *, job_id: str, org_id: str, repo_id: str) -> list[str]:
    """Send the job's stages as a chain; returns their task ids."""
    task_ids = send_stages(job_id, org_id, repo_id, eager=bool(self.request.is_eager))
    log.info("index job sent", stages=len(task_ids))
    return task_ids


def send_stages(job_id: str, org_id: str, repo_id: str, *, eager: bool = False) -> list[str]:
    """Send the stage chain (run it in place when ``eager``: tests, eager dev setups)."""
    kwargs: dict[str, Any] = {"job_id": job_id, "org_id": org_id, "repo_id": repo_id}
    task_ids = [str(uuid.uuid4()) for _ in STAGES]
    signatures = [
        stage.si(**kwargs).set(task_id=task_id)
        for stage, task_id in zip(STAGES, task_ids, strict=True)
    ]
    redis = get_runtime().sync_redis
    key = chain_key(job_id)
    redis.delete(key)
    redis.rpush(key, *task_ids)
    redis.expire(key, CHAIN_TTL_SECONDS)
    workflow = chain(*signatures)
    if eager:
        workflow.apply()
    else:
        workflow.apply_async()
    return task_ids


STALLED_MESSAGE = (
    "Indexing stopped making progress (the worker running it went away). Try again; if it keeps "
    "happening, contact support with the job id."
)


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.SWEEP_STALLED_JOBS.value, system=True)
def sweep_stalled_jobs(self: SenseiTask) -> int:
    """Fail running jobs whose running stage hasn't moved for longer than any task may run
    (the time limit, plus margin): their worker died in a way nothing else reported. Queued jobs
    that never started (their message was lost) are sent again."""
    runtime = get_runtime()
    limit = timedelta(seconds=runtime.settings.worker.time_limit_seconds + STALL_MARGIN_SECONDS)

    async def _sweep() -> int:
        failed = 0
        cutoff = _now() - limit
        outbox = JobOutbox()
        async with self.system_session() as db:
            jobs = await db.scalars(
                select(IndexJob).where(IndexJob.status == JobStatus.RUNNING).with_for_update()
            )
            for job in jobs:
                stage = job.stage
                if stage is None:
                    continue
                entry: dict[str, Any] = (job.stages or {}).get(stage.value, {})
                if entry.get("status") != "running":
                    continue  # between stages: the next task is queued
                moved = entry.get("updated_at") or entry.get("started_at")
                if not isinstance(moved, str) or datetime.fromisoformat(moved) > cutoff:
                    continue
                machine = IndexJobStateMachine(job)
                machine.fail(stage, {"code": "stage_stalled", "message": STALLED_MESSAGE})
                outbox.collect(machine)
                repo = await db.get(Repo, job.repo_id)
                if repo is not None:
                    settle_repo(repo, JobStatus.FAILED)
                failed += 1
        await outbox.publish(runtime.redis)
        return failed

    async def _unsent() -> list[tuple[str, str, str]]:
        cutoff = _now() - timedelta(seconds=REQUEUE_AFTER_SECONDS)
        async with self.system_session() as db:
            rows = await db.execute(
                select(IndexJob.id, IndexJob.org_id, IndexJob.repo_id).where(
                    IndexJob.status == JobStatus.QUEUED, IndexJob.created_at < cutoff
                )
            )
            return [(str(job), str(org), str(repo)) for job, org, repo in rows]

    failed = runtime.run(_sweep())
    if failed:
        log.warning("stalled index jobs failed", jobs=failed)
    for job_id, org_id, repo_id in runtime.run(_unsent()):
        log.warning("queued index job sent again", job_id=job_id)
        send_stages(job_id, org_id, repo_id, eager=bool(self.request.is_eager))
    return failed


def _now() -> datetime:
    return datetime.now(UTC)
