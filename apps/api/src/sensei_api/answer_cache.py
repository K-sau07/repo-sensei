"""The answer cache (4.9): a question asked again at the same commit, in the same mode, is
answered from the answer kept for it, in well under a second, instead of a new run.

A question matches one asked in the same words (normalized: case, spacing, the question mark),
or, when embeddings are configured, one whose embedding is at least :data:`SIMILARITY` alike.
An answer is kept only when it is whole and sure (answered, not abstained, its confidence not
low); it is served only while every file it cites still has the content it cited.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.models import AnswerCacheEntry, ConversationMessage, RepoFile
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


async def lookup(
    db: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
    mode: str,
    question: str,
    embedding: Sequence[float] | None,
) -> AnswerCacheEntry | None:
    """The kept answer for ``question`` (the same words, else the nearest alike enough), when
    its cited files are unchanged; None otherwise."""
    same = (AnswerCacheEntry.snapshot_id == snapshot_id, AnswerCacheEntry.mode == mode)
    found = await db.scalar(
        select(AnswerCacheEntry)
        .where(*same, AnswerCacheEntry.question_key == question_key(question))
        .order_by(AnswerCacheEntry.created_at.desc())
        .limit(1)
    )
    if found is None and embedding is not None:
        distance = AnswerCacheEntry.embedding.op("<=>")(list(embedding))
        row = (
            await db.execute(
                select(AnswerCacheEntry, distance.label("distance"))
                .where(*same, AnswerCacheEntry.embedding.is_not(None))
                .order_by(distance)
                .limit(1)
            )
        ).first()
        if row is not None and 1 - float(row[1]) >= SIMILARITY:
            found = row[0]
    if found is None or not await _unchanged(db, snapshot_id, found.file_hashes):
        return None
    message = await db.get(ConversationMessage, found.message_id)
    return found if message is not None and message.status == "done" else None


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
