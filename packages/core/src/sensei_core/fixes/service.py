"""Fix runs (8.3): made from intake, listed, read.

A member (not a viewer) starts one on a repository they can read; a Sentry delivery starts one
for the org itself (no user). An issue already being worked on isn't started again: a run from
the same GitHub issue or Sentry issue that hasn't finished is returned instead. Frames are
mapped onto the files of the repository's last index.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.errors import NotFoundError
from sensei_core.fixes.tickets import PathIndex, Ticket
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import FixRun, FixSource, FixStage, FixStatus, Repo, RepoFile, Role
from sensei_core.models.fixes import TERMINAL
from sensei_core.orgs.service import OrgAccess, require_role

Clock = Callable[[], datetime]
MAX_PATHS = 200_000
LIST_LIMIT = 100


def utcnow() -> datetime:
    return datetime.now(UTC)


_INDEXES: OrderedDict[uuid.UUID, PathIndex] = OrderedDict()
CACHED_INDEXES = 16


async def repo_paths(db: AsyncSession, repo: Repo) -> list[str]:
    """The files of the repository's last index (none if it has never been indexed)."""
    try:
        snapshot = await resolve_snapshot(db, repo, None)
    except NotFoundError:
        return []
    found = await db.scalars(
        select(RepoFile.path).where(RepoFile.snapshot_id == snapshot.id).limit(MAX_PATHS)
    )
    return list(found)


async def repo_index(db: AsyncSession, repo: Repo) -> PathIndex:
    """The repository's files as a :class:`PathIndex`, built once per snapshot (a few kept):
    the intake page parses as the user types."""
    try:
        snapshot = await resolve_snapshot(db, repo, None)
    except NotFoundError:
        return PathIndex(())
    cached = _INDEXES.get(snapshot.id)
    if cached is not None:
        _INDEXES.move_to_end(snapshot.id)
        return cached
    found = await db.scalars(
        select(RepoFile.path).where(RepoFile.snapshot_id == snapshot.id).limit(MAX_PATHS)
    )
    index = PathIndex(list(found))
    _INDEXES[snapshot.id] = index
    while len(_INDEXES) > CACHED_INDEXES:
        _INDEXES.popitem(last=False)
    return index


class FixService:
    def __init__(self, clock: Clock = utcnow) -> None:
        self._clock = clock

    async def open_for(
        self, db: AsyncSession, repo: Repo, source: FixSource, source_ref: str
    ) -> FixRun | None:
        """A run from the same issue that hasn't finished."""
        found = await db.scalars(
            select(FixRun)
            .where(
                FixRun.repo_id == repo.id,
                FixRun.source == source,
                FixRun.source_ref == source_ref,
                FixRun.status.not_in(TERMINAL),
            )
            .order_by(FixRun.created_at.desc())
            .limit(1)
        )
        return found.first()

    async def create(
        self,
        db: AsyncSession,
        repo: Repo,
        ticket: Ticket,
        *,
        source: FixSource,
        source_ref: str | None = None,
        access: OrgAccess | None = None,
        status: FixStatus = FixStatus.QUEUED,
    ) -> tuple[FixRun, bool]:
        """The run, and whether it is new (False: one from the same issue is going already).
        ``access`` is the person starting it (a member); None for the org itself (Sentry)."""
        if access is not None:
            require_role(access, Role.MEMBER)
        if source_ref is not None:
            existing = await self.open_for(db, repo, source, source_ref)
            if existing is not None:
                return existing, False
        run = FixRun(
            org_id=repo.org_id, repo_id=repo.id, source=source, source_ref=source_ref,
            title=ticket.title, ticket=ticket.model_dump(mode="json"), status=status,
            stage=FixStage.INTAKE, created_by=access.user.id if access else None,
        )  # fmt: skip
        try:
            async with db.begin_nested():  # two deliveries at once: one run (a unique index)
                db.add(run)
                await db.flush()
        except IntegrityError:
            if source_ref is None:
                raise
            existing = await self.open_for(db, repo, source, source_ref)
            if existing is None:
                raise
            return existing, False
        await audit(
            db, AuditAction.FIX_CREATE, actor_user_id=access.user.id if access else None,
            org_id=repo.org_id, target_type="fix_run", target_id=run.id,
            meta={"repo": repo.slug, "source": source.value, "status": status.value},
        )  # fmt: skip
        return run, True

    async def list(
        self, db: AsyncSession, repo: Repo, *, status: FixStatus | None = None
    ) -> list[FixRun]:
        query = select(FixRun).where(FixRun.repo_id == repo.id)
        if status is not None:
            query = query.where(FixRun.status == status)
        found = await db.scalars(
            query.order_by(FixRun.created_at.desc(), FixRun.id.desc()).limit(LIST_LIMIT)
        )
        return list(found)

    async def get(self, db: AsyncSession, org_id: uuid.UUID, fix_id: uuid.UUID) -> FixRun:
        run = await db.get(FixRun, fix_id)
        if run is None or run.org_id != org_id:
            raise NotFoundError("No such fix.", code="fix_not_found")
        return run
