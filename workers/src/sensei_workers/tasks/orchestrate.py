"""Running an index job: its stages as a Celery chain, and the stage that finishes it.

``run_index_job`` turns a queued job into the chain clone → discover → merkle → parse (with
symbols) → chunks → embeddings → summaries → graph → links → finalize. Each stage is its own
task (parse and symbols share one: the symbols come out of the parse), so a failure retries only
that stage. Every
stage checks the job first: a cancelled or failed job makes the rest of the chain do nothing.
The stage task ids are kept in Redis so cancelling can revoke the ones still queued.

``finalize_index`` ends the run (embeddings and summaries are recorded as skipped when the
server has no Voyage or Anthropic key: those stages pass themselves over): the repository
becomes ``ready`` at the indexed commit, and the job succeeds. The chunks of the repository's
other snapshots are pruned in the same transaction (search serves the indexed
commit), with its own rows outside the live generation; its chunks partition is vacuumed after
(a task of its own). A job that reaches finalize straight from symbols (its chain was sent
before the chunks stage existed) gets the rest of the chain sent; so does one that reaches it
before the graph stage or the links stage (sent before that stage existed).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from celery import chain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.indexing.events import JobOutbox
from sensei_core.indexing.jobs import JOB_USAGE_REF, settle_repo
from sensei_core.indexing.snapshots import point_ref, ref_heads
from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.jobs import TaskName, chain_key
from sensei_core.logging import get_logger
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    IndexJob,
    JobKind,
    JobStatus,
    Repo,
    Snapshot,
    Stage,
)
from sensei_core.usage import costs_of
from sensei_engine.chunk.store import prune_chunks
from sensei_engine.incremental import moved_on, previous_of
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.jobstate import job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.tasks.chunk import chunk_snapshot
from sensei_workers.tasks.discover import discover_files
from sensei_workers.tasks.embed import embed_snapshot
from sensei_workers.tasks.graph import build_graph, previous_done
from sensei_workers.tasks.ingest import IngestTask, clone_snapshot
from sensei_workers.tasks.links import build_links, graph_over
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


class _MissingStageError(Exception):
    """The job reached finalize without a stage its chain didn't have (sent before the stage
    existed): from symbols without chunks, or from the stages before the graph without it."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def _sent_once_key(job_id: str, what: str) -> str:
    return f"{chain_key(job_id)}:sent-{what}"


def _send_once(job_id: str, what: str) -> bool:
    """True the first time for this job and ``what`` (until the chain's keys expire)."""
    key = _sent_once_key(job_id, what)
    return bool(get_runtime().sync_redis.set(key, "1", nx=True, ex=CHAIN_TTL_SECONDS))


async def _settle_ref(db: AsyncSession, job: IndexJob, repo: Repo, snapshot: Snapshot) -> int:
    """The run's ref reads its snapshot now. The repository's index is its default branch's: a
    tracked branch's (or a pull request's) run is an overlay beside it (5.4), which neither
    moves it nor prunes; the default branch's prunes what no ref reads. Returns the chunks
    pruned."""
    await point_ref(db, repo, job.ref or repo.default_branch, snapshot)
    if job.ref is None or job.ref == repo.default_branch:
        repo.last_indexed_sha = snapshot.commit_sha
        heads = [s.id for s in (await ref_heads(db, repo)).values()]
        return await prune_chunks(db, repo.id, keep=snapshot.id, also=heads)
    if job.kind is JobKind.INCREMENTAL:  # built on the default branch's index
        base = await previous_of(db, repo, snapshot)
        snapshot.overlay_of = base.id if base is not None else None
    return 0


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
                _require_stages(job)
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
                machine.advance(Stage.FINALIZE)
            ref = job.ref or repo.default_branch
            moved, changed = await moved_on(scope.db, repo, ref, snapshot)
            pruned = await _settle_ref(scope.db, job, repo, snapshot)
            if moved:
                scope.outbox.repo_updated(
                    repo, job=job, ref=ref, sha=snapshot.commit_sha, changed=changed
                )
            settle_repo(repo, JobStatus.SUCCEEDED)
            # What the run was charged (embeddings, summaries), on the job and by kind.
            costs = await costs_of(scope.db, job.org_id, JOB_USAGE_REF, str(job.id))
            job.cost_usd = sum(costs.values(), Decimal(0))
            machine.note(
                Stage.FINALIZE,
                cost_usd=str(job.cost_usd),
                costs={kind.value: str(cost) for kind, cost in sorted(costs.items())},
            )
            machine.log(
                f"Indexed {snapshot.commit_sha[:12]}: {snapshot.file_count or 0:,} files. "
                "The code browser is ready." + _cost_line(job.cost_usd)
            )
            machine.finish()
            return snapshot.commit_sha, pruned

    runtime = get_runtime()
    try:
        finished = runtime.run(_finalize())
    except _MissingStageError as missing:
        eager = bool(self.request.is_eager)
        _send_missing(job_id, org_id, repo_id, missing.what, eager=eager)
        if not eager:
            raise NotYetError(
                f"the {missing.what} stage was sent"
            ) from None  # its finalize ends it
        # Run in place, that chain's finalize found this one holding the lock and skipped: the
        # stage is done now, so this one finishes (unless the stage passed itself over: then it
        # waits like any other).
        try:
            finished = runtime.run(_finalize())
        except _MissingStageError:
            raise NotYetError(f"the {missing.what} stage hasn't run") from None
    if finished is None:
        return None
    sha, pruned = finished
    if pruned:
        request_vacuum(uuid.UUID(repo_id), eager=bool(self.request.is_eager))
    log.info("index job succeeded", commit_sha=sha, pruned_chunks=pruned)
    return sha


def _require_stages(job: IndexJob) -> None:
    """Why a job that isn't at finalize can't finish yet: a stage its chain doesn't have
    (``_MissingStageError``), or one still to run (``NotYetError``). Returns for a job past it."""
    symbols = (job.stages or {}).get("symbols", {})
    if job.stage is Stage.SYMBOLS and symbols.get("status") == "done":
        raise _MissingStageError("chunks")  # its chain has none (see below)
    if previous_done(job) and not _waits_for_more(job):
        raise _MissingStageError("graph")
    if graph_over(job):
        raise _MissingStageError("links")
    if job.stage is None or STAGE_ORDER.index(job.stage) <= STAGE_ORDER.index(Stage.LINKS):
        raise NotYetError("the stages before finalize haven't finished")


def _cost_line(cost: Decimal) -> str:
    if cost >= Decimal("0.01"):
        return f" This index cost ${cost:.2f}."
    return " This index cost less than a cent." if cost > 0 else ""


def _send_missing(job_id: str, org_id: str, repo_id: str, what: str, *, eager: bool) -> None:
    """A chain sent before a stage existed reaches finalize without it: the stages from it on
    are sent, once (NotYetError for every delivery after the first)."""
    stage = MISSING_STAGES[what]
    if not _send_once(job_id, what):
        raise NotYetError(f"the {what} stage hasn't run")
    log.warning("index job without a stage: sending it", job_id=job_id, stage=what)
    try:
        send_stages(job_id, org_id, repo_id, eager=eager, first=STAGES.index(stage))
    except Exception:
        # Not sent: the next delivery (or the sweeper's resend) tries again.
        get_runtime().sync_redis.delete(_sent_once_key(job_id, what))
        raise


def _waits_for_more(job: IndexJob) -> bool:
    """Whether a stage before the graph may still run: summaries or embeddings after the stage
    the job is at, when the server can do them (they pass themselves over when it can't)."""
    runtime = get_runtime()
    if job.stage is Stage.CHUNKS:
        return runtime.embeddings is not None or runtime.anthropic.configured
    if job.stage is Stage.EMBEDDINGS:
        return runtime.anthropic.configured
    return False


def _ready_to_finalize(job: IndexJob) -> bool:
    """The last stage before finalize is over: the links, done or skipped."""
    status = (job.stages or {}).get(job.stage.value if job.stage else "", {}).get("status")
    return job.stage is Stage.LINKS and status in {"done", "skipped"}


STAGES = (
    clone_snapshot,
    discover_files,
    build_merkle,
    parse_snapshot,
    chunk_snapshot,
    embed_snapshot,
    summarize_snapshot,
    build_graph,
    build_links,
    finalize_index,
)
# The stages a chain sent before they existed may lack (see _send_missing).
MISSING_STAGES = {"chunks": chunk_snapshot, "graph": build_graph, "links": build_links}


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
