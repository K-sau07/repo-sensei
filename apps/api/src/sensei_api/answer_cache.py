"""The answer cache (4.9): a question asked again in the same mode is answered from the answer
kept for it, in well under a second, instead of a new run.

A question matches one asked in the same words (normalized: case, spacing, the question mark),
or, when embeddings are configured, one whose embedding is at least :data:`SIMILARITY` alike.
An answer is kept only when it is whole and sure (answered, not abstained, its confidence not
low); it is served only while every file it cites still has the content it cited. That holds
across commits (5.5): an answer kept at an earlier commit is served at a later one when none of
its files changed since (its citations then point at the later commit, at the same lines), and
one whose cited file changed is never served again. Only along the same ref, though: an answer
kept at a branch or a pull request may rest on code the default branch doesn't have (files it
read but didn't cite), so it is served at that ref's later commits, never at another ref's.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import AnswerCacheEntry, ConversationMessage, RepoFile, Snapshot
from sensei_engine.agent.loop import Answer

SIMILARITY = 0.95


def question_key(question: str) -> str:
    normalized = " ".join(question.lower().split()).rstrip(" ?!.")
    return hashlib.sha256(normalized.encode()).hexdigest()


def keepable(answer: Answer) -> bool:
    """Whether the answer may be served again: whole, not abstained, and not of low
    confidence."""
    sure = answer.confidence
    return (
        answer.stopped == "answered"
        and answer.abstained is None
        and sure is not None
        and sure.level != "low"
    )


CANDIDATES = 5  # kept answers tried, nearest first, for the one whose files are unchanged


async def lookup(
    db: AsyncSession,
    *,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    mode: str,
    question: str,
    embedding: Sequence[float] | None,
) -> AnswerCacheEntry | None:
    """The kept answer for ``question`` (the same words, else the nearest alike enough; this
    commit's first, then the latest), when its cited files are as they are at ``snapshot_id``;
    None otherwise."""
    same = (AnswerCacheEntry.repo_id == repo_id, AnswerCacheEntry.mode == mode)
    here = (AnswerCacheEntry.snapshot_id == snapshot_id).desc()
    found = list(
        await db.scalars(
            select(AnswerCacheEntry)
            .where(*same, AnswerCacheEntry.question_key == question_key(question))
            .order_by(here, AnswerCacheEntry.created_at.desc())
            .limit(CANDIDATES)
        )
    )
    if not found and embedding is not None:
        distance = AnswerCacheEntry.embedding.op("<=>")(list(embedding))
        rows = await db.execute(
            select(AnswerCacheEntry, distance.label("distance"))
            .where(*same, AnswerCacheEntry.embedding.is_not(None))
            .order_by(distance, here, AnswerCacheEntry.created_at.desc())
            .limit(CANDIDATES)
        )
        found = [entry for entry, d in rows.tuples() if 1 - float(d) >= SIMILARITY]
    current = await db.get(Snapshot, snapshot_id)
    for entry in found:
        if entry.snapshot_id != snapshot_id:
            if not entry.file_hashes:
                continue  # its files weren't known when kept: nothing to show they are the same
            kept_at = await db.get(Snapshot, entry.snapshot_id)
            if current is None or kept_at is None or kept_at.ref != current.ref:
                continue  # kept at another ref: it may rest on code this one doesn't have
        if not await _unchanged(db, snapshot_id, entry.file_hashes):
            continue  # a file it cites changed since: never served again
        message = await db.get(ConversationMessage, entry.message_id)
        if message is not None and message.status == "done":
            return entry
    return None


async def keep(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    repo_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    mode: str,
    question: str,
    embedding: Sequence[float] | None,
    message_id: uuid.UUID,
    cited: Sequence[str],
) -> None:
    """Keep the answer ``message_id`` for ``question``, with the hashes of the files it cites."""
    hashes = await _hashes(db, snapshot_id, sorted(set(cited)))
    db.add(
        AnswerCacheEntry(
            org_id=org_id,
            repo_id=repo_id,
            snapshot_id=snapshot_id,
            mode=mode,
            question_key=question_key(question),
            question=question,
            embedding=list(embedding) if embedding is not None else None,
            message_id=message_id,
            file_hashes=hashes,
        )
    )
    await db.flush()


async def _hashes(db: AsyncSession, snapshot_id: uuid.UUID, paths: Sequence[str]) -> dict[str, str]:
    if not paths:
        return {}
    rows = await db.execute(
        select(RepoFile.path, RepoFile.content_hash).where(
            RepoFile.snapshot_id == snapshot_id, RepoFile.path.in_(paths)
        )
    )
    return {path: digest.hex() for path, digest in rows.tuples()}


async def _unchanged(db: AsyncSession, snapshot_id: uuid.UUID, hashes: dict[str, str]) -> bool:
    now = await _hashes(db, snapshot_id, sorted(hashes))
    return now == hashes
