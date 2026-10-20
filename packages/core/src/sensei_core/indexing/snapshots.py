"""Which indexed snapshot a ``ref`` means: the last index, a commit (or its prefix), a branch."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.errors import InvalidError, NotFoundError
from sensei_core.models import Repo, Snapshot, SnapshotStatus

_SHA_PREFIX = re.compile(r"[0-9a-f]{7,64}")
MAX_REF_LENGTH = 255


def _not_indexed(ref: str | None) -> NotFoundError:
    if ref is None:
        return NotFoundError("This repository hasn't been indexed yet.", code="repo_not_indexed")
    return NotFoundError(f"Nothing indexed at {ref!r}.", code="ref_not_indexed")


async def resolve_snapshot(db: AsyncSession, repo: Repo, ref: str | None) -> Snapshot:
    """The browsable snapshot for ``ref``: None means the last index; a hex string of 7 or more
    characters is a commit id (or the start of one); anything else is a branch, whose most
    recently indexed commit is used."""
    browsable = select(Snapshot).where(
        Snapshot.repo_id == repo.id,
        Snapshot.status == SnapshotStatus.READY,
        Snapshot.merkle_root.is_not(None),
    )
    if ref is None:
        if repo.last_indexed_sha is None:
            raise _not_indexed(None)
        found = await db.scalar(browsable.where(Snapshot.commit_sha == repo.last_indexed_sha))
        if found is None:
            raise _not_indexed(None)
        return found
    if len(ref) > MAX_REF_LENGTH or not ref.strip():
        raise InvalidError("That isn't a branch or commit.", code="invalid_ref")
    if _SHA_PREFIX.fullmatch(ref):
        matches = list(
            await db.scalars(browsable.where(Snapshot.commit_sha.startswith(ref)).limit(2))
        )
        if len(matches) > 1:
            raise InvalidError(
                f"{ref!r} matches more than one indexed commit; use more characters.",
                code="ambiguous_ref",
            )
        if matches:
            return matches[0]
    found = await db.scalar(
        browsable.where(Snapshot.ref == ref).order_by(Snapshot.ready_at.desc()).limit(1)
    )
    if found is None:
        raise _not_indexed(ref)
    return found
