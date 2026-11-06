"""The staff admin console (10.6.3): the service at a glance, its people and orgs, and index runs
in every org, failed ones retried and stuck ones cancelled.

Reads across orgs go through the ``staff_read`` row-level policy (SELECT only, and only while
the transaction names a user the database says is staff: ``set_staff_context``). The two actions
are an org owner's, done in the run's org by the staff member as its owner would: the same role
checks, the same dispatch, recorded in that org's audit log under the staff member's name.

The read-only switch stops anything new that costs from starting, today, everywhere, as the
spend cap does; lifting it lasts only while today's spend is under the cap (the worker sets it
again within a minute otherwise).

Signed-in non-staff get a 404, as for every staff route.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.auth import CurrentStaff
from sensei_api.deps import DbSession, RedisClient, Writable
from sensei_api.errors import NotFound
from sensei_api.routers.index_jobs import IndexJobOut, _jobs, _service, dispatch_index  # pyright: ignore[reportPrivateUsage]
from sensei_core.db.tenancy import set_org_context, set_staff_context
from sensei_core.errors import ConflictError
from sensei_core.indexing.events import JobOutbox
from sensei_core.jobs import chain_key
from sensei_core.limits import READ_ONLY, read_only
from sensei_core.models import IndexJob, JobStatus, Membership, Org, Repo, Role, UsageEvent, User
from sensei_core.orgs.service import OrgAccess

router = APIRouter(prefix="/internal/admin", tags=["internal"])
MAX_PAGE_SIZE = 100
DAY = timedelta(days=1)


async def staff_db(staff: CurrentStaff, db: DbSession) -> AsyncSession:
    """The request's session, reading every org's rows of the staff tables."""
    await set_staff_context(db, staff.id)
    return db


StaffDb = Annotated[AsyncSession, Depends(staff_db)]


class AdminOverviewOut(BaseModel):
    spent_today_usd: Decimal = Field(description="Every org's metered spend since midnight UTC.")
    cap_usd: Decimal | None = Field(description="The day's cap for all of it (None: no cap).")
    read_only: bool = Field(description="Nothing new that costs starts until midnight UTC.")
    users: int
    orgs: int
    repos: int
    failed_runs_today: int


class AdminUserOut(BaseModel):
    id: uuid.UUID
    login: str
    name: str | None
    is_staff: bool
    deleted: bool
    orgs: int
    created_at: datetime


class AdminOrgOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    plan: str
    members: int
    repos: int
    spent_30d_usd: Decimal
    deleted_at: datetime | None
    created_at: datetime


class AdminJobOut(IndexJobOut):
    org_slug: str
    repo_full_name: str
    repo_slug: str


class ReadOnlyIn(BaseModel):
    on: bool


def _midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@router.get("", response_model=AdminOverviewOut)
async def overview(request: Request, db: StaffDb, redis: RedisClient) -> AdminOverviewOut:
    """The service today (staff)."""
    now = datetime.now(UTC)
    cap = request.app.state.settings.limits.global_daily_spend_cap_usd
    spent = await db.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.created_at >= _midnight(now)
        )
    )
    return AdminOverviewOut(
        spent_today_usd=Decimal(spent or 0),
        cap_usd=cap if cap > 0 else None,
        read_only=await read_only(redis, now=now),
        users=await db.scalar(select(func.count()).select_from(User)) or 0,
        orgs=await db.scalar(select(func.count()).where(Org.deleted_at.is_(None))) or 0,
        repos=await db.scalar(select(func.count()).select_from(Repo)) or 0,
        failed_runs_today=await db.scalar(
            select(func.count()).where(
                IndexJob.status == JobStatus.FAILED, IndexJob.created_at >= _midnight(now)
            )
        )
        or 0,
    )


@router.get("/users", response_model=list[AdminUserOut])
async def users(
    db: StaffDb,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> list[AdminUserOut]:
    """People, newest first; ``q`` matches login or name (staff)."""
    orgs = select(func.count()).where(Membership.user_id == User.id).scalar_subquery()
    query = select(User, orgs).order_by(User.created_at.desc()).limit(limit)
    if q:
        like = f"%{q}%"
        query = query.where(or_(User.login.ilike(like), User.name.ilike(like)))
    return [
        AdminUserOut(
            id=user.id,
            login=user.login,
            name=user.name,
            is_staff=user.is_staff,
            deleted=user.deleted_at is not None,
            orgs=count or 0,
            created_at=user.created_at,
        )
        for user, count in await db.execute(query)
    ]


@router.get("/orgs", response_model=list[AdminOrgOut])
async def orgs(
    db: StaffDb,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> list[AdminOrgOut]:
    """Orgs, newest first, deleted ones too; ``q`` matches slug or name (staff)."""
    since = datetime.now(UTC) - 30 * DAY
    members = select(func.count()).where(Membership.org_id == Org.id).scalar_subquery()
    repos = select(func.count()).where(Repo.org_id == Org.id).scalar_subquery()
    spent = (
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0))
        .where(UsageEvent.org_id == Org.id, UsageEvent.created_at >= since)
        .scalar_subquery()
    )
    query = select(Org, members, repos, spent).order_by(Org.created_at.desc()).limit(limit)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Org.slug.ilike(like), Org.name.ilike(like)))
    return [
        AdminOrgOut(
            id=org.id,
            slug=org.slug,
            name=org.name,
            plan=org.plan,
            members=m or 0,
            repos=r or 0,
            spent_30d_usd=Decimal(s or 0),
            deleted_at=org.deleted_at,
            created_at=org.created_at,
        )
        for org, m, r, s in await db.execute(query)
    ]


def _job_out(job: IndexJob, org: Org, repo: Repo) -> AdminJobOut:
    return AdminJobOut(
        **IndexJobOut.build(job).model_dump(),
        org_slug=org.slug,
        repo_full_name=repo.full_name,
        repo_slug=repo.slug,
    )


@router.get("/jobs", response_model=list[AdminJobOut])
async def jobs(
    db: StaffDb,
    status: Literal["failed", "running", "queued", "any"] = "failed",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> list[AdminJobOut]:
    """Index runs in every org, newest first: failed ones by default (staff)."""
    query = (
        select(IndexJob, Org, Repo)
        .join(Repo, Repo.id == IndexJob.repo_id)
        .join(Org, Org.id == IndexJob.org_id)
        .order_by(IndexJob.created_at.desc())
        .limit(limit)
    )
    if status != "any":
        query = query.where(IndexJob.status == JobStatus(status))
    return [_job_out(job, org, repo) for job, org, repo in await db.execute(query)]


async def _as_owner(
    db: AsyncSession, staff: User, job_id: uuid.UUID
) -> tuple[OrgAccess, Repo, IndexJob]:
    job = await db.get(IndexJob, job_id)
    if job is None:
        raise NotFound("Index run not found.")
    org = await db.get(Org, job.org_id)
    repo = await db.get(Repo, job.repo_id)
    if org is None or repo is None or org.deleted_at is not None:
        raise NotFound("Index run not found.")
    await set_org_context(db, org.id)  # the run's org: its writes are org-scoped as ever
    return OrgAccess(org=org, role=Role.OWNER, user=staff), repo, job


@router.post("/jobs/{job}/retry", response_model=AdminJobOut, dependencies=[Writable])
async def retry_job(
    *,
    request: Request,
    job: uuid.UUID,
    staff: CurrentStaff,
    db: StaffDb,
    redis: RedisClient,
    background: BackgroundTasks,
) -> AdminJobOut:
    """Index the failed run's repository again, as its org's owner would (staff): 409
    ``index_job_running`` while another run is under way, ``not_failed`` unless it failed."""
    access, repo, found = await _as_owner(db, staff, job)
    if found.status is not JobStatus.FAILED:
        raise ConflictError("Only a failed run is retried.", code="not_failed")
    outbox = JobOutbox()
    started = await _service(request).start(db, access, repo, outbox)
    background.add_task(
        dispatch_index, redis, _jobs(request), outbox,
        org_id=repo.org_id, repo_id=repo.id, job_id=started.id,
    )  # fmt: skip
    return _job_out(started, access.org, repo)


@router.post("/jobs/{job}/cancel", response_model=AdminJobOut)
async def cancel_job(
    *,
    request: Request,
    job: uuid.UUID,
    staff: CurrentStaff,
    db: StaffDb,
    redis: RedisClient,
    background: BackgroundTasks,
) -> AdminJobOut:
    """Cancel a queued or running run, as its org's owner would (staff)."""
    access, repo, found = await _as_owner(db, staff, job)
    outbox = JobOutbox()
    cancelled = await _service(request).cancel(db, access, repo, found.id, outbox)

    async def after_commit() -> None:
        await outbox.publish(redis)
        pending = cast(
            "Awaitable[list[str]]",
            redis.lrange(chain_key(found.id), 0, -1),  # pyright: ignore[reportUnknownMemberType]
        )
        await _jobs(request).revoke(list(await pending))

    background.add_task(after_commit)
    return _job_out(cancelled, access.org, repo)


@router.put("/read-only", response_model=AdminOverviewOut)
async def set_read_only(
    body: ReadOnlyIn, request: Request, db: StaffDb, redis: RedisClient
) -> AdminOverviewOut:
    """Stop (``on``) anything new that costs from starting until midnight UTC, everywhere, or
    lift today's stop (staff). Lifted, it comes back within a minute while today's spend is past
    the cap."""
    now = datetime.now(UTC)
    key = READ_ONLY.format(day=now.date().isoformat())
    if body.on:
        lasts = int((_midnight(now) + DAY - now).total_seconds()) + 3600
        await redis.set(key, "staff", ex=lasts)
    else:
        await redis.delete(key)
    return await overview(request, db, redis)
