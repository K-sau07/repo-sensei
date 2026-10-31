"""Snapshot retention (5.2.5): only a snapshot past its time and in no use expires."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.indexing.retention import expired
from sensei_core.models import IndexJob, JobKind, JobStatus, Org, Repo, RepoSource, Snapshot

NOW = datetime(2026, 10, 31, tzinfo=UTC)
OLD = NOW - timedelta(days=60)


@pytest.mark.integration
async def test_only_an_old_snapshot_in_no_use_expires(system_session: AsyncSession) -> None:
    db = system_session
    org = Org(slug=f"org-{uuid.uuid4().hex[:8]}", name="Acme")
    db.add(org)
    await db.flush()
    repo = Repo(
        org_id=org.id,
        source=RepoSource.PUBLIC_URL,
        github_repo_id=77,
        owner="o",
        name="r",
        full_name="o/r",
        slug="r",
        default_branch="main",
        visibility="public",
    )
    db.add(repo)
    await db.flush()

    def taken(sha: str, ref: str, at: datetime) -> Snapshot:
        snapshot = Snapshot(
            org_id=org.id, repo_id=repo.id, ref=ref, commit_sha=sha * 40, created_at=at
        )
        db.add(snapshot)
        return snapshot

    superseded = taken("a", "main", OLD)  # main moved on: it goes
    indexed = taken("b", "main", OLD + timedelta(days=1))  # the repository's index: it stays
    taken("c", "main", NOW - timedelta(days=1))  # recent: it stays (and main's latest)
    branch = taken("d", "release", OLD)  # old, but release's latest: it stays
    building = taken("e", "feature", OLD)  # old, but a run is building it: it stays
    taken("f", "feature", NOW - timedelta(days=2))
    await db.flush()
    repo.last_indexed_sha = indexed.commit_sha
    db.add(
        IndexJob(
            org_id=org.id,
            repo_id=repo.id,
            snapshot_id=building.id,
            kind=JobKind.FULL,
            status=JobStatus.RUNNING,
        )
    )
    await db.flush()
    found = await expired(db, before=NOW - timedelta(days=30))
    assert [s.id for s in found if s.repo_id == repo.id] == [superseded.id]
    assert branch.id not in {s.id for s in found}
