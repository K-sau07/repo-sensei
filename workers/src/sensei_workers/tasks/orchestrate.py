"""Running an index job: its stages as a Celery chain, and the stage that finishes it.

``run_index_job`` turns a queued job into the chain clone → discover → merkle → parse (with
symbols) → chunks → summaries → finalize (later phases add embeddings between chunks and
summaries, then graph and links after them). Each stage is its own task (parse and symbols
share one: the symbols come out of the parse), so a failure retries only that stage. Every
stage checks the job first: a cancelled or failed job makes the rest of the chain do nothing.
The stage task ids are kept in Redis so cancelling can revoke the ones still queued.

``finalize_index`` ends the run: stages this phase doesn't have yet are recorded as skipped (so
are summaries when the server has no Anthropic key: that stage passes itself over), the
repository becomes ``ready`` at the indexed commit, and the job succeeds. The chunks of the
repository's other snapshots are pruned in the same transaction (search serves the indexed
commit), with its own rows outside the live generation; its chunks partition is vacuumed after
(a task of its own). A job that reaches finalize straight from symbols (its chain was sent
before the chunks stage existed) gets the rest of the chain sent.
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
from sensei_engine.chunk.store import prune_chunks
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.jobstate import job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.tasks.chunk import chunk_snapshot
from sensei_workers.tasks.discover import discover_files
from sensei_workers.tasks.ingest import IngestTask, clone_snapshot
from sensei_workers.tasks.maintenance import request_vacuum
from sensei_workers.tasks.merkle import build_merkle
from sensei_workers.tasks.parse import parse_snapshot
from sensei_workers.tasks.summaries import summarize_snapshot

log = get_logger(__name__)

STAGE_ORDER = list(Stage)
CHAIN_TTL_SECONDS = 2 * 86_400
# A running stage with no progress for the task time limit plus this is dead.
STALL_MARGIN_SECONDS = 600
# A job waiting this long for its next task (queued, or between two stages) may have lost its
# message: the chain is sent again, unless it was sent within RESEND_AFTER_SECONDS (a long
# queue is not a lost message, and resending into it only makes it longer).
REQUEUE_AFTER_SECONDS = 600
RESEND_AFTER_SECONDS = 6 * 3600
# A job still waiting between stages after this, resends included, has failed.
GIVE_UP_AFTER_SECONDS = 24 * 3600


class _NoChunksStageError(Exception):
    """The job went from symbols to finalize without a chunks stage."""


def _sent_once_key(job_id: str, what: str) -> str:
    return f"{chain_key(job_id)}:sent-{what}"


def _send_once(job_id: str, what: str) -> bool:
    """True the first time for this job and ``what`` (until the chain's keys expire)."""
    key = _sent_once_key(job_id, what)
    return bool(get_runtime().sync_redis.set(key, "1", nx=True, ex=CHAIN_TTL_SECONDS))


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.FINALIZE_INDEX.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def finalize_index(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> str | None:
    """Finish the job: the repository is ready at the snapshot's commit. Returns the commit."""

    async def _finalize() -> tuple[str, int] | None:
        async with job_scope(self, org_id, uuid.UUID(job_id)) as scope:
            job = scope.job
            if job is None or job.status not in ACTIVE_JOB_STATUSES:
                return None  # finished, failed or cancelled meanwhile
            ready = _ready_to_finalize(job)
            resuming = job.stage is Stage.FINALIZE
            if not ready and not resuming:
                stages = job.stages or {}
                symbols = stages.get("symbols", {})
                if job.stage is Stage.SYMBOLS and symbols.get("status") == "done":
                    raise _NoChunksStageError  # its chain has none (see below)
                if job.stage is None or STAGE_ORDER.index(job.stage) <= STAGE_ORDER.index(
                    Stage.SUMMARIES
                ):
                    raise NotYetError("the stages before finalize haven't finished")
                return None
            repo = await scope.db.get(Repo, job.repo_id)
            snapshot = await scope.db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
            machine = scope.machine
            if repo is None or repo.deleted_at is not None:
                machine.cancel()
                return None
            if snapshot is None:
                raise NotYetError("the job has no snapshot")
            if ready:
                machine.advance(Stage.FINALIZE)  # later phases' stages: skipped for now
            repo.last_indexed_sha = snapshot.commit_sha
            pruned = await prune_chunks(scope.db, repo.id, keep=snapshot.id)
            settle_repo(repo, JobStatus.SUCCEEDED)
            machine.log(
                f"Indexed {snapshot.commit_sha[:12]}: {snapshot.file_count or 0:,} files. "
                "The code browser is ready."
            )
            machine.finish()
            return snapshot.commit_sha, pruned

    runtime = get_runtime()
    try:
        finished = runtime.run(_finalize())
    except _NoChunksStageError:
        eager = bool(self.request.is_eager)
        _send_the_chunks_stage(job_id, org_id, repo_id, eager=eager)
        if not eager:
            raise NotYetError("the chunks stage was sent") from None  # its finalize finishes
        # Run in place, that chain's finalize found this one holding the lock and skipped: the
        # chunks are done now, so this one finishes (unless the chunks stage passed itself
        # over: then it waits like any other).
        try:
            finished = runtime.run(_finalize())
        except _NoChunksStageError:
            raise NotYetError("the chunks stage hasn't run") from None
    if finished is None:
        return None
    sha, pruned = finished
    if pruned:
        request_vacuum(uuid.UUID(repo_id), eager=bool(self.request.is_eager))
    log.info("index job succeeded", commit_sha=sha, pruned_chunks=pruned)
    return sha


def _send_the_chunks_stage(job_id: str, org_id: str, repo_id: str, *, eager: bool) -> None:
    """A chain sent before the chunks stage existed goes from symbols straight to finalize: the
    stages from chunks on are sent, once (NotYetError for every delivery after the first)."""
    if not _send_once(job_id, "chunks"):
        raise NotYetError("the chunks stage hasn't run")
    log.warning("index job without a chunks stage: sending it", job_id=job_id)
    try:
        send_stages(job_id, org_id, repo_id, eager=eager, first=STAGES.index(chunk_snapshot))
    except Exception:
        # Not sent: the next delivery (or the sweeper's resend) tries again.
        get_runtime().sync_redis.delete(_sent_once_key(job_id, "chunks"))
        raise


def _ready_to_finalize(job: IndexJob) -> bool:
    """The last stage before finalize is over: summaries done or skipped, or chunks done when
    the summaries stage passed itself over (no Anthropic key)."""
    status = (job.stages or {}).get(job.stage.value if job.stage else "", {}).get("status")
    if job.stage is Stage.SUMMARIES:
        return status in {"done", "skipped"}
    return job.stage is Stage.CHUNKS and status == "done"


STAGES = (
    clone_snapshot,
    discover_files,
    build_merkle,
    parse_snapshot,
    chunk_snapshot,
    # Embeddings (2.7) go here; summaries follow them.
    summarize_snapshot,
    finalize_index,
)


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


def _sent_key(job_id: str) -> str:
    return f"{chain_key(job_id)}:sent-at"


def send_stages(
    job_id: str, org_id: str, repo_id: str, *, eager: bool = False, first: int = 0
) -> list[str]:
    """Send the stage chain from stage ``first`` (run it in place when ``eager``: tests, eager
    dev setups).

    Sending it again is safe: every stage checks the job first, so stages already done do
    nothing and the run carries on where it stopped. The task ids of every send are kept, so
    cancelling revokes all of them."""
    kwargs: dict[str, Any] = {"job_id": job_id, "org_id": org_id, "repo_id": repo_id}
    stages = STAGES[first:]
    task_ids = [str(uuid.uuid4()) for _ in stages]
    signatures = [
        stage.si(**kwargs).set(task_id=task_id)
        for stage, task_id in zip(stages, task_ids, strict=True)
    ]
    redis = get_runtime().sync_redis
    key = chain_key(job_id)
    redis.rpush(key, *task_ids)
    redis.expire(key, CHAIN_TTL_SECONDS)
    redis.set(_sent_key(job_id), _now().isoformat(), ex=CHAIN_TTL_SECONDS)
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
WAITING_MESSAGE = (
    "Indexing stopped between two steps and didn't pick up again. Try again; if it keeps "
    "happening, contact support with the job id."
)


def _sent_recently(job_id: str, now: datetime) -> bool:
    raw = get_runtime().sync_redis.get(_sent_key(job_id))
    if raw is None:
        return False
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        sent = datetime.fromisoformat(text)
    except ValueError:
        return False
    return now - sent < timedelta(seconds=RESEND_AFTER_SECONDS)


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@celery_app.task(bind=True, base=SenseiTask, name=TaskName.SWEEP_STALLED_JOBS.value, system=True)
def sweep_stalled_jobs(self: SenseiTask) -> int:
    """Look after index jobs nothing is moving any more. Returns how many it failed.

    * A running stage with no sign of life (progress, or a retry it said it's waiting for) for
      longer than any task may run (the time limit, plus margin) lost its worker: it fails.
    * A job waiting for its next task (queued, or between two stages) for a while may have lost
      that message: its chain is sent again, at most every ``RESEND_AFTER_SECONDS``.
    * A job still waiting between stages after ``GIVE_UP_AFTER_SECONDS`` fails.
    """
    runtime = get_runtime()
    limit = runtime.settings.worker.time_limit_seconds + STALL_MARGIN_SECONDS
    now = _now()

    async def _sweep() -> tuple[int, list[tuple[str, str, str]]]:
        failed = 0
        waiting: list[tuple[str, str, str]] = []
        outbox = JobOutbox()
        async with self.system_session() as db:
            jobs = await db.scalars(
                select(IndexJob).where(IndexJob.status.in_(ACTIVE_JOB_STATUSES)).with_for_update()
            )
            for job in jobs:
                ids = (str(job.id), str(job.org_id), str(job.repo_id))
                if job.status is JobStatus.QUEUED:
                    queued_for = (now - job.created_at).total_seconds()
                    if queued_for >= REQUEUE_AFTER_SECONDS:
                        waiting.append(ids)
                    continue
                stage = job.stage
                if stage is None:
                    continue
                entry: dict[str, Any] = (job.stages or {}).get(stage.value, {})
                machine = IndexJobStateMachine(job)
                if entry.get("status") == "running":
                    signs = (
                        _time(entry.get("updated_at") or entry.get("started_at")),
                        _time(entry.get("alive_until")),
                    )
                    last = max((sign for sign in signs if sign is not None), default=None)
                    if last is None or (now - last).total_seconds() <= limit:
                        continue
                    machine.fail(stage, {"code": "stage_stalled", "message": STALLED_MESSAGE})
                else:
                    # Between stages: this one is done and the next hasn't started.
                    finished = _time(entry.get("finished_at"))
                    idle = (now - finished).total_seconds() if finished else None
                    if idle is None or idle < REQUEUE_AFTER_SECONDS:
                        continue
                    if idle < GIVE_UP_AFTER_SECONDS:
                        waiting.append(ids)
                        continue
                    machine.fail_waiting({"code": "stage_stalled", "message": WAITING_MESSAGE})
                outbox.collect(machine)
                repo = await db.get(Repo, job.repo_id)
                if repo is not None:
                    settle_repo(repo, JobStatus.FAILED)
                failed += 1
        await outbox.publish(runtime.redis)
        return failed, waiting

    failed, waiting = runtime.run(_sweep())
    if failed:
        log.warning("stalled index jobs failed", jobs=failed)
    for job_id, org_id, repo_id in waiting:
        if _sent_recently(job_id, now):
            continue
        log.warning("waiting index job sent again", job_id=job_id)
        send_stages(job_id, org_id, repo_id, eager=bool(self.request.is_eager))
    return failed


def _now() -> datetime:
    return datetime.now(UTC)
