"""Pushes (5.1): a push to a tracked branch is noted and its timer set; the pushes of a burst
fold into one pending push, run once; a forced push, or a last index that can't be read, runs in
full."""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.github.webhooks import process_delivery, record_delivery
from sensei_core.indexing import pushes
from sensei_core.indexing.events import JobOutbox
from sensei_core.models import (
    DeliveryStatus,
    GitHubInstallation,
    IndexJob,
    JobKind,
    JobStatus,
    Org,
    Repo,
    RepoSource,
    RepoStatus,
    Snapshot,
)
from sensei_core.models.indexing import SnapshotStatus

INSTALLATION = 4242
GITHUB_REPO = 9001
SHA = ["a" * 40, "b" * 40, "c" * 40, "d" * 40]


class Timers:
    """The timers set, in order, and the refs dropped."""

    def __init__(self) -> None:
        self.set: list[tuple[uuid.UUID, str, str, int]] = []
        self.dropped: list[tuple[uuid.UUID, str]] = []

    async def schedule(
        self, org_id: uuid.UUID, repo_id: uuid.UUID, branch: str, token: str, *, countdown: int
    ) -> None:
        self.set.append((repo_id, branch, token, countdown))

    async def drop(self, org_id: uuid.UUID, repo_id: uuid.UUID, ref: str) -> None:
        self.dropped.append((repo_id, ref))


async def _repo(db: AsyncSession, *, tracked: list[str] | None = None) -> Repo:
    org = Org(slug=f"org-{uuid.uuid4().hex[:8]}", name="Acme")
    db.add(org)
    await db.flush()
    link = GitHubInstallation(
        org_id=org.id,
        installation_id=INSTALLATION,
        account_login="acme-gh",
        account_type="Organization",
        account_id=5150,
        repository_selection="selected",
        permissions={"contents": "read"},
        events=["push"],
    )
    db.add(link)
    await db.flush()
    repo = Repo(
        org_id=org.id,
        installation_id=link.id,
        source=RepoSource.GITHUB_APP,
        github_repo_id=GITHUB_REPO,
        owner="acme-gh",
        name="demo",
        full_name="acme-gh/demo",
        slug="demo",
        default_branch="main",
        visibility="private",
        status=RepoStatus.READY,
        settings={"tracked_branches": tracked or []},
    )
    db.add(repo)
    await db.flush()
    return repo


def _push(ref: str, before: str, after: str, **extra: Any) -> dict[str, Any]:
    return {
        "ref": ref,
        "before": before,
        "after": after,
        "forced": False,
        "deleted": False,
        "repository": {"id": GITHUB_REPO, "full_name": "acme-gh/demo"},
        "installation": {"id": INSTALLATION},
        **extra,
    }


async def _deliver(
    db: AsyncSession,
    redis: FakeAsyncRedis,
    timers: Timers,
    payload: dict[str, Any],
    event: str = "push",
) -> tuple[str, DeliveryStatus | None]:
    delivery_id = uuid.uuid4().hex
    row = await record_delivery(db, delivery_id=delivery_id, event=event, payload=payload)
    assert row is not None
    result = await process_delivery(db, redis, row, timers=timers)
    return delivery_id, result.status


def test_a_push_names_a_branch_or_nothing() -> None:
    assert pushes.branch_of("refs/heads/main") == "main"
    assert pushes.branch_of("refs/heads/feature/x") == "feature/x"
    assert pushes.branch_of("refs/tags/v1") is None
    assert pushes.branch_of("refs/heads/") is None
    assert pushes.branch_of(None) is None


@pytest.mark.integration
async def test_a_burst_of_pushes_is_one_pending_push_whose_last_timer_runs_it(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    repo = await _repo(system_session)
    timers = Timers()
    tokens: list[str] = []
    for before, after in itertools.pairwise(SHA):
        token, status = await _deliver(
            system_session, fake_redis, timers, _push("refs/heads/main", before, after)
        )
        assert status is DeliveryStatus.PROCESSED
        tokens.append(token)
    # Each push set a timer for DEBOUNCE_SECONDS on, with its own token.
    assert timers.set == [(repo.id, "main", t, pushes.DEBOUNCE_SECONDS) for t in tokens]
    # The pushes are one: the last's commit, the first's starting point.
    for early in tokens[:-1]:
        assert await pushes.due(fake_redis, repo.id, "main", early) is None
    pending = await pushes.due(fake_redis, repo.id, "main", tokens[-1])
    assert pending is not None
    assert (pending.after, pending.before, pending.forced) == (SHA[3], SHA[0], False)
    # Its run is queued once; a second start while it is queued waits for it.
    outbox = JobOutbox()
    job = await pushes.start(system_session, repo, "main", pending, outbox)
    assert job is not None
    assert (job.kind, job.ref, job.target_sha) == (JobKind.FULL, "main", SHA[3])  # no index yet
    assert await pushes.running(system_session, repo)
    with pytest.raises(pushes.JobRunningError):
        await pushes.start(system_session, repo, "main", pending, outbox)
    await pushes.done(fake_redis, repo.id, "main", tokens[-1])
    assert await pushes.due(fake_redis, repo.id, "main", tokens[-1]) is None


@pytest.mark.integration
async def test_a_push_after_an_index_is_incremental_and_a_forced_one_full(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    repo = await _repo(system_session)
    system_session.add(
        Snapshot(
            org_id=repo.org_id,
            repo_id=repo.id,
            ref="main",
            commit_sha=SHA[0],
            status=SnapshotStatus.READY,
            merkle_root="00" * 32,
        )
    )
    repo.last_indexed_sha = SHA[0]
    await system_session.flush()
    plain = pushes.Pending(after=SHA[1], before=SHA[0], forced=False, token="t")
    assert await pushes.kind_for(system_session, repo, plain) == (JobKind.INCREMENTAL, None)
    # A forced push among them: the branch's history was rewritten, so all of it is read again.
    timers = Timers()
    await _deliver(system_session, fake_redis, timers, _push("refs/heads/main", SHA[0], SHA[1]))
    token, _ = await _deliver(
        system_session,
        fake_redis,
        timers,
        _push("refs/heads/main", SHA[1], SHA[2], forced=True),
    )
    await _deliver(system_session, fake_redis, timers, _push("refs/heads/main", SHA[2], SHA[3]))
    latest = timers.set[-1][2]
    pending = await pushes.due(fake_redis, repo.id, "main", latest)
    assert pending is not None
    assert pending.forced  # one forced push makes the burst's run full
    kind, why = await pushes.kind_for(system_session, repo, pending)
    assert (kind, why) == (JobKind.FULL, "a forced push rewrote the branch's history")
    job = await pushes.start(system_session, repo, "main", pending, JobOutbox())
    assert job is not None
    assert job.kind is JobKind.FULL
    assert token != latest


@pytest.mark.integration
async def test_a_last_index_that_cant_be_read_runs_in_full(
    system_session: AsyncSession,
) -> None:
    repo = await _repo(system_session)
    repo.last_indexed_sha = SHA[0]  # no snapshot of it
    await system_session.flush()
    pending = pushes.Pending(after=SHA[1], before=SHA[0], forced=False, token="t")
    assert await pushes.kind_for(system_session, repo, pending) == (
        JobKind.FULL,
        "the last index can't be read to compare with",
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "payload",
    [
        _push("refs/tags/v1", SHA[0], SHA[1]),  # a tag
        _push("refs/heads/main", SHA[0], "0" * 40, deleted=True),  # the branch deleted
        _push("refs/heads/feature", SHA[0], SHA[1]),  # a branch it doesn't track
        _push("refs/heads/main", SHA[0], SHA[1], repository={"id": 1}),  # another repository
    ],
)
async def test_pushes_that_index_nothing_are_ignored(
    payload: dict[str, Any], system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    await _repo(system_session)
    timers = Timers()
    _, status = await _deliver(system_session, fake_redis, timers, payload)
    assert status is DeliveryStatus.IGNORED
    assert timers.set == []


@pytest.mark.integration
async def test_a_tracked_branch_is_noted_and_its_run_kept_apart(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    repo = await _repo(system_session, tracked=["release"])
    timers = Timers()
    _, status = await _deliver(
        system_session, fake_redis, timers, _push("refs/heads/release", SHA[0], SHA[1])
    )
    assert status is DeliveryStatus.PROCESSED
    assert [(branch, countdown) for _, branch, _, countdown in timers.set] == [
        ("release", pushes.DEBOUNCE_SECONDS)
    ]
    pending = await pushes.due(fake_redis, repo.id, "release", timers.set[0][2])
    assert pending is not None
    job = await pushes.start(system_session, repo, "release", pending, JobOutbox())
    assert job is not None
    assert (job.ref, job.status) == ("release", JobStatus.QUEUED)
    found = await system_session.scalar(select(IndexJob).where(IndexJob.id == job.id))
    assert found is not None


def _pull(action: str, number: int = 7, sha: str = SHA[1]) -> dict[str, Any]:
    return {
        "action": action,
        "number": number,
        "pull_request": {"number": number, "head": {"sha": sha, "ref": "feature"}},
        "repository": {"id": GITHUB_REPO, "full_name": "acme-gh/demo"},
        "installation": {"id": INSTALLATION},
    }


@pytest.mark.integration
async def test_a_pull_request_is_indexed_as_an_overlay_only_when_the_repository_asks(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    repo = await _repo(system_session)
    timers = Timers()
    _, status = await _deliver(system_session, fake_redis, timers, _pull("opened"), "pull_request")
    assert status is DeliveryStatus.IGNORED  # not asked for
    assert timers.set == []
    repo.settings = {**repo.settings, "index_pull_requests": True}
    await system_session.flush()
    _, status = await _deliver(
        system_session, fake_redis, timers, _pull("synchronize"), "pull_request"
    )
    assert status is DeliveryStatus.PROCESSED
    [(repo_id, ref, token, countdown)] = timers.set
    assert (repo_id, ref, countdown) == (repo.id, "pull/7", pushes.DEBOUNCE_SECONDS)
    pending = await pushes.due(fake_redis, repo.id, "pull/7", token)
    assert pending is not None
    assert pending.after == SHA[1]
    # Closed: nothing pending any more, and its overlay is dropped.
    _, status = await _deliver(system_session, fake_redis, timers, _pull("closed"), "pull_request")
    assert status is DeliveryStatus.PROCESSED
    assert await pushes.due(fake_redis, repo.id, "pull/7", token) is None
    assert timers.dropped == [(repo.id, "pull/7")]
    # A push to a branch named like a pull request's ref isn't taken for one.
    _, status = await _deliver(
        system_session, fake_redis, timers, _push("refs/heads/pull/7", SHA[0], SHA[2])
    )
    assert status is DeliveryStatus.IGNORED


@pytest.mark.integration
async def test_pushes_delivered_out_of_order_index_the_latest_commit(
    fake_redis: FakeAsyncRedis,
) -> None:
    repo_id = uuid.uuid4()
    # A pushes to B, then B to C; their deliveries come C first.
    late = await pushes.note(
        fake_redis, repo_id, "main", after=SHA[2], before=SHA[1], forced=False, token="c"
    )
    assert (late.after, late.before) == (SHA[2], SHA[1])
    folded = await pushes.note(
        fake_redis, repo_id, "main", after=SHA[1], before=SHA[0], forced=False, token="b"
    )
    assert (folded.after, folded.before) == (SHA[2], SHA[0])  # still C, from A
    assert folded.token == "b"  # the last delivered's timer runs it


@pytest.mark.integration
async def test_a_forced_rollback_within_the_wait_indexes_where_it_went_back_to(
    fake_redis: FakeAsyncRedis,
) -> None:
    repo_id = uuid.uuid4()
    await pushes.note(
        fake_redis, repo_id, "main", after=SHA[1], before=SHA[0], forced=False, token="b"
    )
    # Then forced back: B → A. Not an earlier push delivered late: the branch is at A now.
    back = await pushes.note(
        fake_redis, repo_id, "main", after=SHA[0], before=SHA[1], forced=True, token="a"
    )
    assert (back.after, back.forced) == (SHA[0], True)
    # A forced push delivered late is still put before the pending one.
    other = uuid.uuid4()
    await pushes.note(
        fake_redis, other, "main", after=SHA[2], before=SHA[1], forced=False, token="c"
    )
    folded = await pushes.note(
        fake_redis, other, "main", after=SHA[1], before=SHA[0], forced=True, token="b"
    )
    assert (folded.after, folded.before, folded.forced) == (SHA[2], SHA[0], True)
