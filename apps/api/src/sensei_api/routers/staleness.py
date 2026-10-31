"""Staleness (5.5): whether what an answer, a diagram or a trace cited is still as it was.

A citation names its commit and file (``<sha>:<path>``); it is stale when that file's content at
the repository's index now (or at ``ref``) differs from its content at the cited commit, or the
file is gone. Content is compared by hash, so a file that moved on and came back is current
again. A cited commit whose snapshot is no longer kept can't be compared: that is said, not
guessed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from sensei_api.deps import DbSession
from sensei_api.repos import RepoDep
from sensei_api.routers.browse import RefParam
from sensei_core.errors import InvalidError
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import RepoFile, Snapshot

router = APIRouter(prefix="/orgs/{org}/repos/{repo}", tags=["staleness"])

MAX_CITATIONS = 200
Reason = Literal["unchanged", "changed", "deleted", "unknown"]


class CitedOut(BaseModel):
    sha: str
    path: str
    stale: bool | None  # None: the cited commit can't be compared (its snapshot is gone)
    reason: Reason


class StalenessOut(BaseModel):
    sha: str  # what they were compared with
    citations: list[CitedOut]


def _parse(cited: str) -> tuple[str, str]:
    sha, _, path = cited.partition(":")
    if not (7 <= len(sha) <= 64) or not path or len(path) > 1024:
        message = f"{cited[:80]!r} isn't a citation (<sha>:<path>)."
        raise InvalidError(message, code="invalid_citation")
    return sha.lower(), path


@router.get("/staleness", response_model=StalenessOut)
async def staleness(
    *,
    repo: RepoDep,
    db: DbSession,
    citations: Annotated[list[str], Query(max_length=MAX_CITATIONS)],
    ref: RefParam = None,
) -> StalenessOut:
    """Each citation (``citations=<sha>:<path>``, repeated) compared with the index now."""
    wanted = [_parse(cited) for cited in citations]
    head = await resolve_snapshot(db, repo, ref)
    paths = sorted({path for _, path in wanted})
    now = dict(
        (
            await db.execute(
                select(RepoFile.path, RepoFile.content_hash).where(
                    RepoFile.snapshot_id == head.id, RepoFile.path.in_(paths)
                )
            )
        )
        .tuples()
        .all()
    )
    shas = sorted({sha for sha, _ in wanted})
    then: dict[tuple[str, str], bytes] = {}
    known: set[str] = set()
    for sha in shas:
        snapshot = await db.scalar(
            select(Snapshot)
            .where(Snapshot.repo_id == repo.id, Snapshot.commit_sha.startswith(sha))
            .limit(1)
        )
        if snapshot is None:
            continue
        known.add(sha)
        rows = await db.execute(
            select(RepoFile.path, RepoFile.content_hash).where(
                RepoFile.snapshot_id == snapshot.id, RepoFile.path.in_(paths)
            )
        )
        for path, digest in rows.tuples():
            then[(sha, path)] = digest
    out: list[CitedOut] = []
    for sha, path in wanted:
        if sha not in known or (sha, path) not in then:
            out.append(CitedOut(sha=sha, path=path, stale=None, reason="unknown"))
        elif path not in now:
            out.append(CitedOut(sha=sha, path=path, stale=True, reason="deleted"))
        elif now[path] != then[(sha, path)]:
            out.append(CitedOut(sha=sha, path=path, stale=True, reason="changed"))
        else:
            out.append(CitedOut(sha=sha, path=path, stale=False, reason="unchanged"))
    return StalenessOut(sha=head.commit_sha, citations=out)
