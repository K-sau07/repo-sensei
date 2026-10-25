"""The summaries stage (2.8): what every source file and its symbols do, in the domain's words,
written by Claude in one Message Batch; then the snapshot's glossary and directory rollups.

It follows embeddings (done or skipped; or chunks, when the embeddings stage passed itself over
for want of a Voyage key: :func:`_after_done`), over the files that have symbols, and goes
through these steps, each recorded before the next starts so a task that stops part way resumes
where it was:

1. **Send**: the requests are built from the working copy, the cost estimated (the batch and the
   rollups after it) and checked against the org's monthly cap under the org's cost lock, in
   the transaction that reserves the batch (:func:`sensei_engine.llm.summaries.reserve_batch`);
   then the batch is sent. Over the cap, the stage is skipped (the index stays usable).
2. **Wait**: each attempt asks how far the batch got, reports it as progress and comes back
   later (a retry with a countdown; the stall sweep is told when). No worker waits on a batch.
3. **Collect**: the results are claimed in the transaction that records the stage's progress
   (:func:`sensei_engine.llm.batches.collect`: exactly once), and the summaries written as the
   snapshot's ``summaries.json`` artifact, with the glossary. Summary-view chunks, embedded,
   join them here with 2.6 and 2.7.
4. **Retry** the files without a usable answer once each, at full price; rewrite both.
5. **Rollups**: a summary per directory, kept as ``rollups.json``; the stage completes, which
   unlocks the wiki.

Steps 4 and 5 pay as they go, so each is marked started before its first call: a task that
stops part way through one doesn't make (and pay for) its calls again. What it cut short stays
detectably undone: the files without a summary (``skipped`` in the artifact), no rollups, the
marker left at ``retrying`` or ``started``, and the stage's details say it is ``partial``. The
wiki counts only what was written; the next index summarizes everything again.

Summaries are optional: with no Anthropic key the stage is passed over, and when Claude can't
do the work (refused requests, out of tries on an outage, nothing summarized) it is skipped
with the reason, never failing the index.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    CodeNode,
    LLMBatch,
    LLMBatchStatus,
    Repo,
    RepoFile,
    Snapshot,
    Stage,
)
from sensei_core.storage import Bucket, artifact_key
from sensei_engine.llm import batches, rollups
from sensei_engine.llm.anthropic_client import LLMError, LLMUsage, Role, tier_for
from sensei_engine.llm.glossary import build_glossary, save_glossary
from sensei_engine.llm.ledger import usage_ledger
from sensei_engine.llm.summaries import (
    KIND,
    CostCapExceeded,
    CostEstimate,
    FileInput,
    FileSummary,
    SummaryRun,
    SymbolInput,
    build_requests,
    check_budget,
    collect_summaries,
    estimate_cost,
    from_json,
    reserve_batch,
    retry_failed,
    to_json,
)
from sensei_engine.snapshots import ArchiveTooLargeError
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError
from sensei_workers.ingest.cache import repo_lock
from sensei_workers.jobstate import JobScope, job_scope
from sensei_workers.runtime import get_runtime
from sensei_workers.tasks.discover import SnapshotUnavailableError, local_worktree
from sensei_workers.tasks.ingest import IngestTask

log = get_logger(__name__)

AFTER = Stage.EMBEDDINGS  # the stage before this one
STAGE = Stage.SUMMARIES
REF_TYPE = "index_job"  # the batch and the usage belong to the job
POLL_SECONDS = 60.0
# Waiting out a batch row whose send may or may not have reached the provider.
UNSETTLED_SECONDS = batches.MATCH_WINDOW.total_seconds()
# Attempts that may end on an Anthropic outage before the stage gives up (skipped).
MAX_UNAVAILABLE = 8
UNAVAILABLE_SECONDS = 120.0
SUMMARIES_ARTIFACT = "summaries.json"
ROLLUPS_ARTIFACT = "rollups.json"
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024

# What the stage records in its details, step by step ("phase").
COLLECTED = "collected"
RETRYING = "retrying"
RETRIED = "retried"
# details["rollups"]: the directory calls began; their artifact was written.
ROLLUPS_STARTED = "started"
ROLLUPS_WRITTEN = "written"


@dataclass(frozen=True, slots=True)
class Target:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str
    job_id: uuid.UUID
    phase: str | None  # the last step done (None: nothing collected yet)
    frameworks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Wait:
    """Come back in ``seconds``."""

    seconds: float


@dataclass(frozen=True, slots=True)
class FileRow:
    path: str
    language: str
    symbols: tuple[SymbolInput, ...]


def _now() -> datetime:
    return datetime.now(UTC)


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _about(value: Decimal) -> str:
    """An estimate, as said in the job's log."""
    return "under $0.01" if 0 < value < Decimal("0.01") else f"about {_money(value)}"


# ---------------------------------------------------------------- the job


async def _begin(task: IngestTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or resume) the job's summaries stage; None if there is nothing to do."""
    async with job_scope(task, org_id, job_id) as scope:
        db, job = scope.db, scope.job
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None
        stages = job.stages or {}
        ready = _after_done(job.stage, stages)
        resuming = job.stage is STAGE and stages.get(STAGE.value, {}).get("status") == "running"
        if not ready and not resuming:
            order = list(Stage)
            if job.stage is None or order.index(job.stage) <= order.index(AFTER):
                raise NotYetError(f"the {AFTER.value} stage hasn't finished")
            return None  # past this stage already (a duplicate delivery)
        repo = await db.get(Repo, job.repo_id)
        machine = scope.machine
        if repo is None or repo.deleted_at is not None:
            machine.cancel()
            return None
        snapshot = await db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
        if snapshot is None:
            raise NotYetError("the job has no snapshot")
        configured = get_runtime().anthropic.configured
        if ready:
            if not configured:
                machine.log("File summaries are off: this server has no Anthropic API key.")
                return None  # finalize passes the stage over
            machine.advance(STAGE)
        elif not configured:
            _skip(scope, "not_configured", "this server no longer has an Anthropic API key")
            return None
        details: dict[str, Any] = stages.get(STAGE.value, {}).get("details", {})
        found = (snapshot.summary or {}).get("frameworks", [])
        frameworks = tuple(sorted({str(f.get("framework")) for f in found if f.get("framework")}))
        return Target(
            repo.org_id,
            repo.id,
            snapshot.id,
            snapshot.commit_sha,
            job_id,
            details.get("phase"),
            frameworks,
        )


def _after_done(stage: Stage | None, stages: Mapping[str, Any]) -> bool:
    """Whether the stage before this one is over: embeddings done or skipped, or chunks done
    when there are no embeddings to wait for (no Voyage key: that stage passes itself over)."""
    status = stages.get(stage.value, {}).get("status") if stage is not None else None
    if stage is AFTER:
        return status in {"done", "skipped"}
    return stage is Stage.CHUNKS and status == "done" and get_runtime().embeddings is None


def _skip(scope: JobScope, reason: str, why: str, **details: Any) -> None:
    scope.machine.skip(STAGE, reason=reason, **details)
    scope.machine.log(f"File summaries skipped: {why}.", level="warn")


@asynccontextmanager
async def _stage(task: IngestTask, target: Target) -> AsyncGenerator[JobScope]:
    """The job, locked, if it is still running the summaries stage (else the task stops)."""
    async with job_scope(task, target.org_id, target.job_id) as scope:
        scope.require(STAGE)
        yield scope


async def _give_up(task: IngestTask, target: Target, reason: str, why: str) -> None:
    async with _stage(task, target) as scope:
        _skip(scope, reason, why)


async def _unavailable(task: IngestTask, target: Target, exc: LLMError) -> Wait | None:
    """An Anthropic outage (:attr:`LLMError.transient`): try again later, or give the stage up after
    :data:`MAX_UNAVAILABLE` attempts that met one."""
    async with _stage(task, target) as scope:
        job = scope.require(STAGE)
        details = (job.stages or {}).get(STAGE.value, {}).get("details", {})
        count = int(details.get("unavailable", 0)) + 1
        if count >= MAX_UNAVAILABLE:
            _skip(scope, "llm_unavailable", f"Claude was unreachable ({exc.message})")
            return None
        scope.machine.note(STAGE, unavailable=count)
        scope.machine.keep_alive(STAGE, _now() + timedelta(seconds=UNAVAILABLE_SECONDS * 2))
    return Wait(UNAVAILABLE_SECONDS)


# ---------------------------------------------------------------- inputs


async def _file_rows(task: IngestTask, target: Target) -> list[FileRow]:
    """The snapshot's files that have symbols, in path order, with their symbols."""
    async with task.org_session(target.org_id) as db:
        files = {
            file_id: (path, language)
            for file_id, path, language in await db.execute(
                select(RepoFile.id, RepoFile.path, RepoFile.language).where(
                    RepoFile.snapshot_id == target.snapshot_id
                )
            )
        }
        nodes = await db.execute(
            select(
                CodeNode.file_id,
                CodeNode.symbol_key,
                CodeNode.kind,
                CodeNode.name,
                CodeNode.start_line,
                CodeNode.end_line,
                CodeNode.signature,
            ).where(CodeNode.snapshot_id == target.snapshot_id)
        )
        symbols: dict[uuid.UUID, list[SymbolInput]] = {}
        for file_id, key, kind, name, start, end, signature in nodes:
            symbols.setdefault(file_id, []).append(
                SymbolInput(key, kind, name, start, end, signature)
            )
    rows = [
        FileRow(files[file_id][0], files[file_id][1] or "text", tuple(found))
        for file_id, found in symbols.items()
        if file_id in files
    ]
    return sorted(rows, key=lambda row: row.path)


def _inputs(target: Target, rows: Sequence[FileRow]) -> list[FileInput]:
    """The files' requests' inputs, their content read from the working copy (call outside the
    event loop: the copy may be unpacked from the archive)."""
    runtime = get_runtime()
    with repo_lock(runtime.settings.ingest.cache_dir, str(target.repo_id)):
        root = local_worktree(target)
        return [
            FileInput(
                row.path,
                row.language,
                (root / row.path).read_text(encoding="utf-8", errors="replace"),
                row.symbols,
                target.frameworks,
            )
            for row in rows
            if (root / row.path).is_file()
        ]


# ---------------------------------------------------------------- 1-3: send, wait, collect


async def _job_batch(db: AsyncSession, job_id: uuid.UUID) -> LLMBatch | None:
    """The job's summaries batch (the latest that didn't fail)."""
    return await db.scalar(
        select(LLMBatch)
        .where(
            LLMBatch.kind == KIND,
            LLMBatch.ref_type == REF_TYPE,
            LLMBatch.ref_id == str(job_id),
            LLMBatch.status != LLMBatchStatus.FAILED,
        )
        .order_by(LLMBatch.created_at.desc(), LLMBatch.id.desc())
        .limit(1)
    )


async def _send(task: IngestTask, target: Target, files: Sequence[FileInput]) -> Wait | None:
    """Estimate, check the cap and reserve under the org's lock, then send. None when the stage
    was skipped."""
    runtime = get_runtime()
    client, costs = runtime.anthropic, runtime.costs
    requests = build_requests(files)
    estimate = await estimate_cost(client, [request for _, request in requests], costs)
    model = client.model_for(tier_for(Role.SUMMARY))
    above = rollups.estimate_cost([file.path for file in files], costs, model)
    total = CostEstimate(
        estimate.requests + above.calls,
        estimate.input_tokens + above.input_tokens,
        estimate.output_tokens + above.output_tokens,
        estimate.cost_usd + above.cost_usd,
        estimate.counted,
    )
    async with _stage(task, target) as scope:
        try:
            row = await reserve_batch(
                scope.db, client, requests, total, org_id=target.org_id,
                cap_usd=runtime.settings.pricing.org_monthly_cap_usd,
                batch_cost_usd=estimate.cost_usd, ref_type=REF_TYPE, ref_id=str(target.job_id),
            )  # fmt: skip
        except CostCapExceeded as exc:
            _skip(
                scope,
                "cost_cap",
                f"they would cost {_about(total.cost_usd)}, and the organization has used"
                f" {_money(exc.spent)} of its {_money(exc.cap)} monthly budget",
                estimate_usd=str(total.cost_usd),
                spent_usd=str(exc.spent),
                cap_usd=str(exc.cap),
            )
            return None
        machine = scope.machine
        machine.note(STAGE, files=len(requests), estimate_usd=str(total.cost_usd))
        machine.update_progress(STAGE, 0, len(requests))
        machine.log(
            f"Summarizing {len(requests):,} files in one batch"
            f" ({_about(total.cost_usd)} with the directory summaries)."
        )
        batch_id = row.id
    try:
        async with task.org_session(target.org_id) as db:
            await batches.send(db, client, batch_id, requests)
    except LLMError as exc:
        if exc.transient:
            raise  # it may have gone out: the row stays to be settled
        async with task.org_session(target.org_id) as db:
            sent = await db.get(LLMBatch, batch_id)
            if sent is not None:
                sent.status = LLMBatchStatus.FAILED
        await _give_up(task, target, "llm_error", f"Claude refused the batch ({exc.message})")
        return None
    return Wait(POLL_SECONDS)


async def _settle(task: IngestTask, target: Target, row: LLMBatch) -> Wait:
    """A batch row still ``submitting``: the send failed or stopped part way. Once old enough it
    is matched to the provider's batch, or marked failed (the next attempt sends again)."""
    async with task.org_session(target.org_id) as db:
        await batches.reconcile(db, get_runtime().anthropic)
        current = await db.get(LLMBatch, row.id)
        unsettled = current is not None and current.status is LLMBatchStatus.SUBMITTING
    if unsettled:
        async with _stage(task, target) as scope:
            until = _now() + timedelta(seconds=UNSETTLED_SECONDS * 2)
            scope.machine.keep_alive(STAGE, until)
        return Wait(UNSETTLED_SECONDS)
    return Wait(1.0)


async def _poll(task: IngestTask, target: Target, row: LLMBatch) -> Wait | None:
    """Ask how far the batch got; None once it has ended."""
    async with task.org_session(target.org_id) as db:
        current = await db.get(LLMBatch, row.id)
        if current is None:  # pragma: no cover - rows go with the org
            msg = f"batch {row.id} is gone"
            raise LookupError(msg)
        await batches.refresh(db, get_runtime().anthropic, current)
        ended = current.status is LLMBatchStatus.ENDED
        done = current.succeeded + current.errored + current.canceled + current.expired
        total = current.request_count
    async with _stage(task, target) as scope:
        previous = int((scope.require(STAGE).stages or {}).get(STAGE.value, {}).get("done") or 0)
        scope.machine.update_progress(STAGE, min(max(done, previous), total), total)
        if not ended:
            until = _now() + timedelta(seconds=POLL_SECONDS * 2)
            scope.machine.keep_alive(STAGE, until)
    return None if ended else Wait(POLL_SECONDS)


async def _node_ids(db: AsyncSession, snapshot_id: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = await db.execute(
        select(CodeNode.symbol_key, CodeNode.id).where(CodeNode.snapshot_id == snapshot_id)
    )
    return dict(rows.tuples().all())


async def _store(
    db: AsyncSession,
    target: Target,
    summaries: Mapping[str, FileSummary],
    skipped: Mapping[str, str],
) -> None:
    """Write the summaries artifact and replace the glossary (in ``db``'s transaction: a failed
    commit leaves an artifact the next attempt overwrites)."""
    storage = await get_runtime().storage()
    key = artifact_key(target.org_id, target.repo_id, target.commit_sha, SUMMARIES_ARTIFACT)
    data = to_json(summaries, skipped)
    await storage.put_bytes(Bucket.ARTIFACTS, key, data, content_type="application/json")
    entries = build_glossary(summaries.values(), await _node_ids(db, target.snapshot_id))
    await save_glossary(
        db,
        org_id=target.org_id,
        repo_id=target.repo_id,
        snapshot_id=target.snapshot_id,
        entries=entries,
    )


async def _collect(
    task: IngestTask, target: Target, row: LLMBatch, files: Sequence[FileInput]
) -> SummaryRun:
    """Claim the ended batch and store what it produced, in the transaction that records it."""
    runtime = get_runtime()
    async with _stage(task, target) as scope:
        current = await scope.db.get(LLMBatch, row.id)
        if current is None:  # pragma: no cover - rows go with the org
            msg = f"batch {row.id} is gone"
            raise LookupError(msg)
        run = await collect_summaries(
            scope.db, runtime.anthropic, current, files, calculator=runtime.costs,
            org_id=target.org_id, ref_type=REF_TYPE, ref_id=str(target.job_id),
        )  # fmt: skip
        await _store(scope.db, target, run.summaries, run.skipped)
        machine = scope.machine
        total = current.request_count
        machine.update_progress(STAGE, total, total)
        machine.note(
            STAGE,
            phase=COLLECTED,
            summarized=len(run.summaries),
            skipped=len(run.skipped),
            dropped_keys=run.dropped_keys,
        )
    return run


# ---------------------------------------------------------------- 4-5: retry, rollups


async def _load(target: Target) -> tuple[dict[str, FileSummary], dict[str, str]]:
    storage = await get_runtime().storage()
    key = artifact_key(target.org_id, target.repo_id, target.commit_sha, SUMMARIES_ARTIFACT)
    data = await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=MAX_ARTIFACT_BYTES)
    if data is None:
        msg = "the summaries artifact is missing"
        raise NotYetError(msg)
    return from_json(data)


async def _retry(
    task: IngestTask, target: Target, run: SummaryRun, files: Sequence[FileInput]
) -> None:
    """Send each file without a summary once more (if the cost guard lets them), storing each
    summary as it comes (a stop part way keeps those paid for), then everything again."""
    runtime = get_runtime()
    retried = [file for file in files if file.path in run.skipped]

    async def started() -> None:
        # Once the guard let them through, before the first paid call (estimating is free).
        async with _stage(task, target) as scope:
            scope.machine.note(STAGE, phase=RETRYING, retried=len(retried))

    async def kept(_: FileSummary) -> None:
        async with _stage(task, target) as scope:
            await _store(scope.db, target, run.summaries, run.skipped)
            scope.machine.note(STAGE, summarized=len(run.summaries), skipped=len(run.skipped))

    @asynccontextmanager
    async def sessions() -> AsyncGenerator[AsyncSession]:
        async with task.org_session(target.org_id) as db:
            yield db

    await retry_failed(
        sessions, runtime.anthropic, run, retried, calculator=runtime.costs,
        org_id=target.org_id, cap_usd=runtime.settings.pricing.org_monthly_cap_usd,
        ref_type=REF_TYPE, ref_id=str(target.job_id), before_calls=started, on_summary=kept,
    )  # fmt: skip
    async with _stage(task, target) as scope:
        await _store(scope.db, target, run.summaries, run.skipped)
        scope.machine.note(
            STAGE,
            phase=RETRIED,
            summarized=len(run.summaries),
            skipped=len(run.skipped),
            retried=len(retried),
        )


async def _within_cap(task: IngestTask, target: Target, paths: Sequence[str]) -> bool:
    """Whether the rollups' estimate passes the cost guard (checked again now that the batch's
    real cost is in the ledger; calls made at once aren't reserved, the lock covers the check)."""
    runtime = get_runtime()
    model = runtime.anthropic.model_for(tier_for(Role.SUMMARY))
    above = rollups.estimate_cost(list(paths), runtime.costs, model)
    estimate = CostEstimate(above.calls, above.input_tokens, above.output_tokens, above.cost_usd, 0)
    try:
        async with task.org_session(target.org_id) as db:
            await check_budget(
                db, target.org_id, estimate, runtime.settings.pricing.org_monthly_cap_usd
            )
    except CostCapExceeded as exc:
        async with _stage(task, target) as scope:
            scope.machine.note(STAGE, rollups="cost_cap", rollups_estimate_usd=str(above.cost_usd))
            scope.machine.log(
                f"Directory summaries skipped: they would cost {_about(above.cost_usd)},"
                f" and the organization has used {_money(exc.spent)} of its"
                f" {_money(exc.cap)} monthly budget.",
                level="warn",
            )
        return False
    return True


async def _rollups(task: IngestTask, target: Target, summaries: Mapping[str, FileSummary]) -> int:
    """Summarize the directories (within the cap, once), keep them as an artifact and complete
    the stage. Started before by this job and never written, they are left out."""
    async with _stage(task, target) as scope:
        details = (scope.require(STAGE).stages or {}).get(STAGE.value, {}).get("details", {})
        state = details.get("rollups")
    if state == ROLLUPS_WRITTEN:
        written = int(details.get("directories", 0))
    elif state == ROLLUPS_STARTED:
        # Written by this job before it stopped, or cut short: left out, not paid for twice.
        written = await _rollups_written(target)
        if written is not None:
            async with _stage(task, target) as scope:
                scope.machine.note(STAGE, rollups=ROLLUPS_WRITTEN)
        written = written or 0
    else:
        written = await _summarize_directories(task, target, summaries)
    async with _stage(task, target) as scope:
        machine = scope.machine
        details = (scope.require(STAGE).stages or {}).get(STAGE.value, {}).get("details", {})
        machine.note(STAGE, directories=written)
        skipped = int(details.get("skipped", 0))
        machine.log(
            f"Summarized {len(summaries):,} files and {written:,} directories"
            + (f"; {skipped:,} files couldn't be summarized." if skipped else ".")
        )
        partial = _partial(details)
        if partial:
            machine.note(STAGE, partial=partial)
            machine.log(
                f"Summaries are partial: {_cut_short(partial)} stopped part way and weren't"
                " paid for twice; the next index summarizes them again.",
                level="warn",
            )
        machine.complete(STAGE)
    return len(summaries)


def _partial(details: Mapping[str, Any]) -> dict[str, Any]:
    """What a stop cut short, by the markers left: the files whose retries were under way
    (still without a summary), and the directory summaries."""
    partial: dict[str, Any] = {}
    if details.get("phase") == RETRYING and int(details.get("skipped", 0)):
        partial["files"] = int(details.get("skipped", 0))
    if details.get("rollups") == ROLLUPS_STARTED:
        partial["directories"] = True
    return partial


def _cut_short(partial: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if partial.get("files"):
        parts.append(f"the retries of {partial['files']:,} files")
    if partial.get("directories"):
        parts.append("the directory summaries")
    return " and ".join(parts)


async def _rollups_written(target: Target) -> int | None:
    """How many directories this job's rollups artifact has, if this job wrote one (another
    job's, for the same commit, doesn't count)."""
    storage = await get_runtime().storage()
    key = artifact_key(target.org_id, target.repo_id, target.commit_sha, ROLLUPS_ARTIFACT)
    data = await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=MAX_ARTIFACT_BYTES)
    if data is None:
        return None
    found = json.loads(data)
    if found.get("job_id") != str(target.job_id):
        return None
    return len(found.get("directories", []))


async def _summarize_directories(
    task: IngestTask, target: Target, summaries: Mapping[str, FileSummary]
) -> int:
    """The directories' summaries, written as the rollups artifact (none over the cap)."""
    runtime = get_runtime()

    async def bill(usage: LLMUsage) -> None:
        # Recorded as each call is answered: a task stopped part way has billed these.
        async with task.org_session(target.org_id) as db:
            sink = usage_ledger(
                db, runtime.costs, org_id=target.org_id, ref_type=REF_TYPE,
                ref_id=str(target.job_id),
            )  # fmt: skip
            await sink(usage)

    if not await _within_cap(task, target, list(summaries)):
        return 0
    async with _stage(task, target) as scope:
        scope.machine.note(STAGE, rollups=ROLLUPS_STARTED)
    found = await rollups.summarize_directories(
        runtime.anthropic, {path: s.summary for path, s in summaries.items()}, on_usage=bill
    )
    storage = await runtime.storage()
    key = artifact_key(target.org_id, target.repo_id, target.commit_sha, ROLLUPS_ARTIFACT)
    data = rollups.to_json(found, job_id=str(target.job_id)).encode()
    await storage.put_bytes(Bucket.ARTIFACTS, key, data, content_type="application/json")
    async with _stage(task, target) as scope:
        scope.machine.note(STAGE, rollups=ROLLUPS_WRITTEN, directories=len(found))
    return len(found)


# ---------------------------------------------------------------- the task


async def _summaries(
    task: IngestTask, target: Target, files: Sequence[FileInput]
) -> Wait | SummaryRun | None:
    """Steps 1 to 3: a Wait to come back later, the collected run, or None (skipped)."""
    async with task.org_session(target.org_id) as db:
        row = await _job_batch(db, target.job_id)
    if row is None:
        if not files:
            return SummaryRun()
        return await _send(task, target, files)
    if row.status is LLMBatchStatus.SUBMITTING:
        return await _settle(task, target, row)
    if row.status is not LLMBatchStatus.ENDED:
        wait = await _poll(task, target, row)
        if wait is not None:
            return wait
    return await _collect(task, target, row, files)


async def _after_collect(
    task: IngestTask, target: Target, run: SummaryRun | None, files: Sequence[FileInput] | None
) -> int | None:
    """Steps 4 and 5 (from the stored summaries when ``run`` is None)."""
    if run is None:
        summaries, skipped = await _load(target)
        run = SummaryRun(summaries=summaries, skipped=skipped)
    # Retries cut short (phase still "retrying") aren't made again: _rollups reports them.
    if target.phase not in (RETRYING, RETRIED) and run.skipped and files is not None:
        await _retry(task, target, run, files)
    if not run.summaries:
        await _give_up(task, target, "nothing_summarized", "no file got a usable summary")
        return None
    return await _rollups(task, target, run.summaries)


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.SUMMARIZE_FILES.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def summarize_snapshot(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> int | None:
    """Summarize the job's snapshot; returns how many files got a summary (None: the stage was
    passed over or skipped, or the job moved on)."""
    runtime = get_runtime()
    target = runtime.run(_begin(self, org_id, uuid.UUID(job_id)))
    if target is None:
        return None
    try:
        files: list[FileInput] | None = None
        run: SummaryRun | None = None
        if target.phase is None:
            files = _inputs(target, runtime.run(_file_rows(self, target)))
            outcome = runtime.run(_summaries(self, target, files))
            if isinstance(outcome, Wait):
                raise self.retry(countdown=outcome.seconds, max_retries=None)
            if outcome is None:
                return None
            run = outcome
        elif target.phase == COLLECTED:
            files = _inputs(target, runtime.run(_file_rows(self, target)))
        written = runtime.run(_after_collect(self, target, run, files))
    except LLMError as exc:
        if not exc.transient:
            runtime.run(_give_up(self, target, "llm_error", f"Claude refused ({exc.message})"))
            return None
        wait = runtime.run(_unavailable(self, target, exc))
        if wait is None:
            return None
        raise self.retry(countdown=wait.seconds, max_retries=None) from exc
    except (SnapshotUnavailableError, ArchiveTooLargeError):
        message = "the snapshot of this commit can't be read"
        runtime.run(_give_up(self, target, "snapshot_unavailable", message))
        return None
    if written is not None:
        log.info("files summarized", files=written)
    return written
