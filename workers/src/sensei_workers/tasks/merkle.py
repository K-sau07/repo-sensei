"""The merkle stage: the snapshot's Merkle tree, for cheap change detection between commits.

``build_merkle`` runs after discover. It builds the tree from the snapshot's file rows (no disk
access, so any worker can run it), stores it next to the snapshot's other artifacts, records the
root hash on the snapshot and, when the repository was indexed at another commit before, notes
what changed since (see :func:`sensei_engine.ingest.merkle.diff`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from sensei_core.indexing.state import IndexJobStateMachine
from sensei_core.jobs import TaskName
from sensei_core.logging import get_logger
from sensei_core.models import (
    ACTIVE_JOB_STATUSES,
    IndexJob,
    JobStatus,
    Repo,
    RepoFile,
    Snapshot,
    Stage,
)
from sensei_core.storage import Bucket, artifact_key
from sensei_engine.ingest.merkle import (
    ARTIFACT_NAME,
    MAX_STORED_BYTES,
    CorruptTreeError,
    Leaf,
    MerkleTree,
    build_tree,
    diff,
)
from sensei_workers.app import celery_app
from sensei_workers.base import NotYetError, SenseiTask
from sensei_workers.runtime import get_runtime
from sensei_workers.tasks.ingest import IngestTask

log = get_logger(__name__)

# Merkle stage progress, in steps: files loaded, tree built and stored, recorded.
STEPS = 3


@dataclass(frozen=True, slots=True)
class Target:
    org_id: uuid.UUID
    repo_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str
    previous_sha: str | None  # the commit indexed last time, if another one


async def _begin(task: SenseiTask, org_id: str, job_id: uuid.UUID) -> Target | None:
    """Start (or resume) the job's merkle stage; None if there is nothing to do."""
    async with task.org_session(org_id) as db:
        job = await db.scalar(select(IndexJob).where(IndexJob.id == job_id).with_for_update())
        if job is None or job.status not in ACTIVE_JOB_STATUSES:
            return None  # finished, failed or cancelled meanwhile
        stages = job.stages or {}
        machine = IndexJobStateMachine(job)
        discover_done = (
            job.stage is Stage.DISCOVER and stages.get("discover", {}).get("status") == "done"
        )
        resuming = job.stage is Stage.MERKLE and stages["merkle"].get("status") == "running"
        if not discover_done and not resuming:
            if job.stage in (None, Stage.CLONE, Stage.DISCOVER):
                raise NotYetError("the discover stage hasn't finished")
            return None  # past this stage already (a duplicate delivery)
        repo = await db.get(Repo, job.repo_id)
        if repo is None or repo.deleted_at is not None:
            machine.cancel()
            return None
        snapshot = await db.get(Snapshot, job.snapshot_id) if job.snapshot_id else None
        if snapshot is None:
            raise NotYetError("the job has no snapshot yet")
        if discover_done:
            machine.advance(Stage.MERKLE)
        previous = repo.last_indexed_sha
        return Target(
            org_id=repo.org_id,
            repo_id=repo.id,
            snapshot_id=snapshot.id,
            commit_sha=snapshot.commit_sha,
            previous_sha=previous if previous and previous != snapshot.commit_sha else None,
        )


async def _step(task: SenseiTask, org_id: str, job_id: uuid.UUID, done: int) -> None:
    async with task.org_session(org_id) as db:
        job = await db.scalar(select(IndexJob).where(IndexJob.id == job_id).with_for_update())
        if job is None or job.status is not JobStatus.RUNNING or job.stage is not Stage.MERKLE:
            return
        previous = int((job.stages or {}).get("merkle", {}).get("done") or 0)
        IndexJobStateMachine(job).update_progress(Stage.MERKLE, max(done, previous), STEPS)


async def _leaves(task: SenseiTask, target: Target) -> list[Leaf]:
    async with task.org_session(target.org_id) as db:
        rows = await db.execute(
            select(RepoFile.path, RepoFile.content_hash, RepoFile.fast_hash, RepoFile.size).where(
                RepoFile.snapshot_id == target.snapshot_id
            )
        )
        return [Leaf(path, content, fast, size) for path, content, fast, size in rows]


async def _store(target: Target, tree: MerkleTree) -> int:
    storage = await get_runtime().storage()
    data = tree.to_bytes()
    key = artifact_key(target.org_id, target.repo_id, target.commit_sha, ARTIFACT_NAME)
    await storage.put_bytes(Bucket.ARTIFACTS, key, data, content_type="application/zstd")
    return len(data)


async def _previous(target: Target) -> MerkleTree | None:
    if target.previous_sha is None:
        return None
    storage = await get_runtime().storage()
    key = artifact_key(target.org_id, target.repo_id, target.previous_sha, ARTIFACT_NAME)
    try:
        data = await storage.get_bytes(Bucket.ARTIFACTS, key, max_bytes=MAX_STORED_BYTES)
        return MerkleTree.from_bytes(data) if data is not None else None
    except (CorruptTreeError, ValueError):
        log.warning("previous merkle tree unreadable", commit_sha=target.previous_sha)
        return None


async def _finish(
    task: SenseiTask, target: Target, job_id: uuid.UUID, root: str, details: dict[str, Any]
) -> bool:
    async with task.org_session(target.org_id) as db:
        job = await db.scalar(select(IndexJob).where(IndexJob.id == job_id).with_for_update())
        if job is None or job.status is not JobStatus.RUNNING or job.stage is not Stage.MERKLE:
            return False
        snapshot = await db.get(Snapshot, target.snapshot_id)
        if snapshot is not None:
            snapshot.merkle_root = root
        machine = IndexJobStateMachine(job)
        machine.update_progress(Stage.MERKLE, STEPS, STEPS)
        machine.note(Stage.MERKLE, root=root, **details)
        machine.complete(Stage.MERKLE)
        return True


@celery_app.task(
    bind=True,
    base=IngestTask,
    name=TaskName.BUILD_MERKLE.value,
    requires=("job_id", "org_id", "repo_id"),
    idempotent_on=("job_id",),
)
def build_merkle(self: IngestTask, *, job_id: str, org_id: str, repo_id: str) -> str | None:
    """Build and store the snapshot's Merkle tree; returns its root hash (hex)."""
    runtime = get_runtime()
    job = uuid.UUID(job_id)
    target = runtime.run(_begin(self, org_id, job))
    if target is None:
        return None
    tree = build_tree(runtime.run(_leaves(self, target)))
    runtime.run(_step(self, org_id, job, 1))
    details: dict[str, Any] = {
        "files": len(tree.files),
        "directories": len(tree.dirs),
        "bytes": runtime.run(_store(target, tree)),
    }
    runtime.run(_step(self, org_id, job, 2))
    previous = runtime.run(_previous(target))
    if previous is not None:
        changes = diff(previous, tree)
        details["since"] = {
            "commit_sha": target.previous_sha,
            "added": len(changes.added),
            "modified": len(changes.modified),
            "deleted": len(changes.deleted),
            "renamed": len(changes.renamed),
        }
    root = tree.root.hex()
    if not runtime.run(_finish(self, target, job, root, details)):
        return None
    log.info("merkle tree stored", files=len(tree.files), root=root)
    return root
