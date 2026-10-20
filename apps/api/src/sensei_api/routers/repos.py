"""Repositories: what the org's GitHub installations offer, connected repositories, imports."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response, status
from pydantic import BaseModel, Field

from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoAccessDep, RepoDep, RepoServiceDep, checked
from sensei_core.jobs import RepoJobs
from sensei_core.models import Repo, RepoSource, RepoStatus
from sensei_core.repos.service import RepoSettings

router = APIRouter(prefix="/orgs/{org}", tags=["repos"])


class RepoOut(BaseModel):
    id: uuid.UUID
    slug: str = Field(description="The repository's name in URLs: /<org>/<slug>.")
    full_name: str
    owner: str
    name: str
    source: RepoSource
    visibility: str
    default_branch: str
    status: RepoStatus
    size_kb: int
    languages: dict[str, int] = Field(
        description="Bytes of code per language, under GitHub's language names: GitHub's counts "
        "until the first index, then repo-sensei's (generated, vendored and documentation files "
        "left out)."
    )
    settings: RepoSettings
    last_indexed_sha: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, repo: Repo) -> RepoOut:
        return cls(
            id=repo.id,
            slug=repo.slug,
            full_name=repo.full_name,
            owner=repo.owner,
            name=repo.name,
            source=repo.source,
            visibility=repo.visibility,
            default_branch=repo.default_branch,
            status=repo.status,
            size_kb=repo.size_kb,
            languages=repo.languages,
            settings=RepoSettings.model_validate(repo.settings or {}),
            last_indexed_sha=repo.last_indexed_sha,
            created_at=repo.created_at,
            updated_at=repo.updated_at,
        )


class AvailableRepoOut(BaseModel):
    github_repo_id: int
    full_name: str
    private: bool
    default_branch: str
    description: str | None
    language: str | None
    pushed_at: str | None
    account_login: str = Field(description="The GitHub account whose installation covers it.")
    connected: bool


class AvailablePage(BaseModel):
    items: list[AvailableRepoOut]
    page: int
    per_page: int
    total: int


class ConnectRepo(BaseModel):
    github_repo_id: int = Field(gt=0)


class ImportRepo(BaseModel):
    url: str = Field(min_length=1, max_length=500)


class EstimateRequest(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    github_repo_id: int | None = Field(default=None, gt=0)


class EstimateOut(BaseModel):
    full_name: str
    private: bool
    size_kb: int
    languages: dict[str, int]
    files: int = Field(description="Estimated number of source files.")
    tokens: int = Field(description="Estimated tokens to embed.")
    index_minutes: float
    cost_usd: Decimal = Field(description="Estimated one-time cost of the first full index.")
    heuristic: bool = Field(default=True, description="Estimates, not measurements.")


class RepoUpdate(BaseModel):
    tracked_branches: list[str] | None = None
    ignore_globs: list[str] | None = None
    auto_index: bool | None = None


def _jobs(request: Request) -> RepoJobs:
    jobs: RepoJobs = request.app.state.repo_jobs
    return jobs


@router.get("/github/repos", response_model=AvailablePage)
async def available_repos(
    *,
    access: OrgAccessDep,
    db: DbSession,
    repos: RepoServiceDep,
    q: Annotated[str, Query(max_length=100)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 30,
) -> AvailablePage:
    """Repositories the org's GitHub installations can read (admins), marking connected ones."""
    found = await repos.available(db, access, query=q)
    start = (page - 1) * per_page
    items = [
        AvailableRepoOut(
            github_repo_id=r.github_repo_id,
            full_name=r.full_name,
            private=r.private,
            default_branch=r.default_branch,
            description=r.description,
            language=r.language,
            pushed_at=r.pushed_at,
            account_login=r.account_login,
            connected=r.connected,
        )
        for r in found[start : start + per_page]
    ]
    return AvailablePage(items=items, page=page, per_page=per_page, total=len(found))


@router.post("/repos", response_model=RepoOut, status_code=status.HTTP_201_CREATED)
async def connect_repo(
    body: ConnectRepo, access: OrgAccessDep, db: DbSession, repos: RepoServiceDep
) -> RepoOut:
    """Connect a repository one of the org's installations can read (admins)."""
    return RepoOut.build(await repos.connect(db, access, body.github_repo_id))


@router.post("/repos/import", response_model=RepoOut, status_code=status.HTTP_201_CREATED)
async def import_repo(
    body: ImportRepo, access: OrgAccessDep, db: DbSession, repos: RepoServiceDep
) -> RepoOut:
    """Import a public GitHub repository by URL (admins). Private ones answer 422
    ``repo_not_public``: they connect through the GitHub App."""
    return RepoOut.build(await repos.import_public(db, access, body.url))


@router.post("/repos/estimate", response_model=EstimateOut)
async def estimate_repo(
    body: EstimateRequest, access: OrgAccessDep, db: DbSession, repos: RepoServiceDep
) -> EstimateOut:
    """Size, index time and cost before connecting, by public URL or GitHub repository id."""
    facts, estimate = await repos.estimate(
        db, access, url=body.url, github_repo_id=body.github_repo_id
    )
    return EstimateOut(
        full_name=str(facts.body.get("full_name")),
        private=bool(facts.body.get("private")),
        size_kb=facts.size_kb,
        languages=facts.languages,
        files=estimate.files,
        tokens=estimate.tokens,
        index_minutes=estimate.index_minutes,
        cost_usd=estimate.cost_usd,
    )


@router.get("/repos", response_model=list[RepoOut])
async def list_repos(
    access: OrgAccessDep, db: DbSession, repos: RepoServiceDep, readers: RepoAccessDep
) -> list[RepoOut]:
    """Connected repositories the signed-in user can read on GitHub."""
    visible = await checked(readers, access.user, await repos.list(db, access))
    return [RepoOut.build(repo) for repo in visible]


@router.get("/repos/{repo}", response_model=RepoOut)
async def get_repo(repo: RepoDep) -> RepoOut:
    return RepoOut.build(repo)


@router.patch("/repos/{repo}", response_model=RepoOut)
async def update_repo(
    body: RepoUpdate, repo: RepoDep, access: OrgAccessDep, db: DbSession, repos: RepoServiceDep
) -> RepoOut:
    """Tracked branches, ignore patterns and automatic indexing (admins)."""
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    return RepoOut.build(await repos.update_settings(db, access, repo, changes))


@router.delete("/repos/{repo}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_repo(
    *,
    request: Request,
    repo: RepoDep,
    access: OrgAccessDep,
    db: DbSession,
    repos: RepoServiceDep,
    readers: RepoAccessDep,
    background: BackgroundTasks,
) -> Response:
    """Disconnect (admins): hidden at once, its data purged in the background."""
    repo_id = await repos.disconnect(db, access, repo)
    await readers.forget_repo(repo_id)
    # After the response, so after the commit: the purge always finds the repository deleted.
    background.add_task(_jobs(request).purge, repo_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
