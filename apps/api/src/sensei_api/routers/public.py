"""The public gallery (10.2): the repositories the explore org publishes, for anyone, signed in
or not (``/explore`` in the web app).

The explore org is the operator's (``SENSEI_EXPLORE_ORG``); its admins publish repositories it
imported by their public URL (``PUT /orgs/{org}/repos/{repo}/published``). One is shown once it
is indexed, at its last index, while it is still public: a repository that went private on
GitHub (access lost) or was disconnected is gone from the gallery at once. With no explore org
the gallery is empty and every page not found.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.deps import DbSession, RedisClient
from sensei_api.routers.feature_maps import FeatureMapList, maps_of
from sensei_api.routers.overview import OverviewOut, overview_of
from sensei_core.config import ExploreSettings
from sensei_core.db.tenancy import set_org_context
from sensei_core.errors import DomainError, NotFoundError
from sensei_core.indexing.jobs import repo_summaries
from sensei_core.indexing.snapshots import resolve_snapshot
from sensei_core.models import Org, Repo, Snapshot
from sensei_core.repos.access import open_to_all

router = APIRouter(prefix="/public", tags=["public"])

MAX_REPOS = 60  # the gallery's, newest first
# Indexed snapshots don't change: a page may be kept a minute, and served stale while
# fetched again.
CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=600"
NAME = r"^[A-Za-z0-9_.-]+$"  # a GitHub owner's or repository's name

Owner = Annotated[str, Path(max_length=100, pattern=NAME)]
Name = Annotated[str, Path(max_length=100, pattern=NAME)]


class PublicRepoOut(BaseModel):
    owner: str
    name: str
    full_name: str
    default_branch: str
    languages: dict[str, int]  # bytes of code per language, at the last index
    sha: str  # the commit it was indexed at
    indexed_at: datetime | None
    files: int | None
    loc: int | None
    summary: str | None  # what it does, from its summaries (None: not summarized)
    published_at: datetime


class PublicRepoList(BaseModel):
    items: list[PublicRepoOut]


class PublicRepoPage(BaseModel):
    repo: PublicRepoOut
    overview: OverviewOut  # its index run left out (the org's)


@router.get("/repos", response_model=PublicRepoList)
async def public_repos(
    *, request: Request, response: Response, db: DbSession, redis: RedisClient
) -> PublicRepoList:
    """The gallery: the published repositories that are indexed, newest first."""
    response.headers["Cache-Control"] = CACHE_CONTROL
    if await _explore_org(request, db) is None:
        return PublicRepoList(items=[])
    repos = list(
        await db.scalars(
            _published().order_by(Repo.published_at.desc(), Repo.full_name).limit(MAX_REPOS)
        )
    )
    shown = [repo for repo in repos if open_to_all(repo)]
    summaries = await repo_summaries(db, shown)
    items: list[PublicRepoOut] = []
    for repo in shown:
        snapshot = summaries[repo.id].snapshot
        if snapshot is None:
            continue
        found = await overview_of(request, repo, db, redis, snapshot)
        items.append(_out(repo, snapshot, found))
    return PublicRepoList(items=items)


@router.get("/repos/{owner}/{name}", response_model=PublicRepoPage)
async def public_repo(
    *,
    request: Request,
    response: Response,
    db: DbSession,
    redis: RedisClient,
    owner: Owner,
    name: Name,
) -> PublicRepoPage:
    """A published repository at its last index: what it does, how it's laid out, its feature
    maps and questions worth asking."""
    repo, snapshot = await published_repo(request, db, owner, name)
    found = await overview_of(request, repo, db, redis, snapshot)
    response.headers["Cache-Control"] = CACHE_CONTROL
    return PublicRepoPage(
        repo=_out(repo, snapshot, found), overview=found.model_copy(update={"index": None})
    )


@router.get("/repos/{owner}/{name}/feature-maps", response_model=FeatureMapList)
async def public_feature_maps(
    *, request: Request, response: Response, db: DbSession, owner: Owner, name: Name
) -> FeatureMapList:
    """A published repository's feature maps at its last index, by path."""
    _, snapshot = await published_repo(request, db, owner, name)
    response.headers["Cache-Control"] = CACHE_CONTROL
    return await maps_of(db, snapshot)


async def published_repo(
    request: Request, db: AsyncSession, owner: str, name: str
) -> tuple[Repo, Snapshot]:
    """The published repository ``owner/name`` at its last index, the transaction scoped to
    the explore org; 404 ``public_repo_not_found`` for one not shown (also to ask about it)."""
    if await _explore_org(request, db) is None:
        raise _not_found()
    repo = await db.scalar(
        _published()
        .where(func.lower(Repo.full_name) == f"{owner}/{name}".lower())
        .order_by(Repo.published_at.desc())
        .limit(1)
    )
    if repo is None or not open_to_all(repo):
        raise _not_found()
    try:
        snapshot = await resolve_snapshot(db, repo, None)
    except DomainError:  # its last index's snapshot is gone: not shown until indexed again
        raise _not_found() from None
    return repo, snapshot


async def _explore_org(request: Request, db: AsyncSession) -> Org | None:
    """The explore org (the transaction scoped to it), if there is one."""
    explore: ExploreSettings = request.app.state.settings.explore
    if not explore.org:
        return None
    org = await db.scalar(select(Org).where(Org.slug == explore.org, Org.deleted_at.is_(None)))
    if org is None:
        return None
    await set_org_context(db, org.id)
    return org


def _published() -> Select[tuple[Repo]]:
    return select(Repo).where(
        Repo.deleted_at.is_(None),
        Repo.published_at.is_not(None),
        Repo.last_indexed_sha.is_not(None),
    )


def _out(repo: Repo, snapshot: Snapshot, overview: OverviewOut) -> PublicRepoOut:
    return PublicRepoOut(
        owner=repo.owner,
        name=repo.name,
        full_name=repo.full_name,
        default_branch=repo.default_branch,
        languages=repo.languages,
        sha=snapshot.commit_sha,
        indexed_at=snapshot.ready_at,
        files=snapshot.file_count,
        loc=snapshot.loc,
        summary=overview.summary.text if overview.summary is not None else None,
        published_at=repo.published_at or repo.updated_at,  # only published ones are read
    )


def _not_found() -> NotFoundError:
    return NotFoundError("Repository not found.", code="public_repo_not_found")
