"""The Codebase Galaxy's semantic layout (6.9), made once per snapshot and kept.

Sent when an index run finishes with its summaries embedded (:func:`request_galaxy`, from
finalize), or by the API when one is asked for and isn't kept (an older snapshot). It reads the
snapshot's summary chunks' embeddings and its glossary, lays the files out
(:func:`sensei_engine.galaxy.layout_galaxy`: UMAP, CPU-bound, off the event loop) and keeps the
result as the ``galaxy-semantic.msgpack`` artifact, which the API serves. A snapshot without
summary embeddings has none to make.
"""

from __future__ import annotations

import asyncio
import gzip
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import cast

import msgpack
from sqlalchemy import select

from sensei_core.jobs import TaskName, galaxy_key
from sensei_core.logging import get_logger
from sensei_core.models import CodeChunk, CodeNode, GlossaryTerm, RepoFile, Snapshot
from sensei_core.storage import Bucket, artifact_key
from sensei_engine.galaxy import file_vectors, layout_galaxy
from sensei_workers.app import celery_app
from sensei_workers.base import SenseiTask
from sensei_workers.runtime import get_runtime

log = get_logger(__name__)

ARTIFACT = "galaxy-semantic.msgpack"
KEY_TTL_SECONDS = 15 * 60  # a request sent and not yet done: not sent again
_pack = cast("Callable[[object], bytes]", msgpack.packb)  # pyright: ignore[reportUnknownMemberType]


def galaxy_artifact(snapshot: Snapshot) -> str:
    return artifact_key(snapshot.org_id, snapshot.repo_id, snapshot.commit_sha, ARTIFACT)


def request_galaxy(org_id: str, repo_id: str, snapshot_id: str, *, eager: bool = False) -> bool:
    """Have the snapshot's galaxy made (at once, in place, when ``eager``); False if one is on
    its way already. Never raises: the API asks again when it's wanted."""
    runtime = get_runtime()
    key = galaxy_key(snapshot_id)
    try:
        if not runtime.sync_redis.set(key, "1", nx=True, ex=KEY_TTL_SECONDS):
            return False
        kwargs = {"org_id": org_id, "repo_id": repo_id, "snapshot_id": snapshot_id}
        try:
            if eager:
                layout_galaxy_task.apply(kwargs=kwargs)
            else:
                layout_galaxy_task.apply_async(kwargs=kwargs)
        except Exception:
            runtime.sync_redis.delete(key)
            raise
    except Exception:
        log.warning("couldn't ask for a galaxy", snapshot_id=snapshot_id, exc_info=True)
        return False
    return True


async def _make(task: SenseiTask, org_id: uuid.UUID, snapshot_id: uuid.UUID) -> int | None:
    async with task.org_session(org_id) as db:
        snapshot = await db.get(Snapshot, snapshot_id)
        if snapshot is None:
            return None
        rows = (
            await db.execute(
                select(CodeChunk.path, CodeChunk.embedding).where(
                    CodeChunk.repo_id == snapshot.repo_id,
                    CodeChunk.snapshot_id == snapshot.id,
                    CodeChunk.view == "summary",
                    CodeChunk.embedding.is_not(None),
                )
            )
        ).all()
        named = (
            await db.execute(
                select(GlossaryTerm.term, RepoFile.path)
                .select_from(GlossaryTerm)
                .join(CodeNode, CodeNode.id == GlossaryTerm.symbol_ids.any_())
                .join(RepoFile, RepoFile.id == CodeNode.file_id)
                .where(GlossaryTerm.snapshot_id == snapshot.id)
            )
        ).all()
        name = galaxy_artifact(snapshot)
    vectors = file_vectors([(r.path, list(r.embedding)) for r in rows if r.embedding is not None])
    if not vectors:
        return None
    terms: defaultdict[str, list[str]] = defaultdict(list)
    for term, path in named:
        terms[path].append(term)
    galaxy = await asyncio.to_thread(layout_galaxy, vectors, terms)
    storage = await get_runtime().storage()
    await storage.put_bytes(Bucket.ARTIFACTS, name, gzip.compress(_pack(galaxy.as_json())))
    return len(galaxy.stars)


@celery_app.task(
    bind=True,
    base=SenseiTask,
    name=TaskName.LAYOUT_GALAXY.value,
    requires=("org_id", "repo_id", "snapshot_id"),
)
def layout_galaxy_task(self: SenseiTask, *, org_id: str, repo_id: str, snapshot_id: str) -> int:
    """Make and keep the snapshot's semantic galaxy; returns its stars (0: nothing to lay out)."""
    runtime = get_runtime()
    try:
        made = runtime.run(_make(self, uuid.UUID(org_id), uuid.UUID(snapshot_id)))
    finally:
        runtime.sync_redis.delete(galaxy_key(snapshot_id))
    log.info("galaxy laid out", repo_id=repo_id, stars=made or 0)
    return made or 0
