"""Repository dependencies: the service, access mirroring, and ``/orgs/{org}/repos/{repo}``."""

from __future__ import annotations

from typing import Annotated

import httpx
import structlog
from fastapi import Depends, Path, Request
from redis.asyncio import Redis

from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep
from sensei_api.routers.auth import GitHubOAuthDep
from sensei_api.routers.github import GitHubAppDep
from sensei_core.auth.github_login import fresh_access_token
from sensei_core.config import Settings
from sensei_core.errors import NotFoundError
from sensei_core.github.client import Credential, GitHubClient, GitHubError
from sensei_core.github.etags import RedisETagCache
from sensei_core.github.oauth import OAuthError
from sensei_core.models import Repo, User
from sensei_core.repos.access import RepoAccess
from sensei_core.repos.service import (
    GitHubReauthRequiredError,
    GitHubUnavailableError,
    RepoService,
)
from sensei_core.security.crypto import TokenCipher
from sensei_core.usage import CostCalculator


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _client(request: Request) -> GitHubClient:
    """A GitHub client with the shared (encrypted) ETag cache: conditional requests that come
    back 304 don't count against GitHub's rate limit."""
    redis: Redis = request.app.state.redis
    http: httpx.AsyncClient = request.app.state.http
    cipher: TokenCipher = request.app.state.token_cipher
    return GitHubClient(_settings(request).github, http, etags=RedisETagCache(redis, cipher))


def get_repo_service(request: Request, app: GitHubAppDep) -> RepoService:
    state = request.app.state
    calc: CostCalculator | None = getattr(state, "cost_calculator", None)
    if calc is None:
        calc = CostCalculator.from_settings(_settings(request).pricing)
        state.cost_calculator = calc
    return RepoService(app, _client(request), calc)


RepoServiceDep = Annotated[RepoService, Depends(get_repo_service)]


def get_repo_access(request: Request, db: DbSession, oauth: GitHubOAuthDep) -> RepoAccess:
    cipher: TokenCipher = request.app.state.token_cipher

    async def token_for(user: User) -> str:
        if oauth is None:
            raise OAuthError("github_not_configured", "GitHub sign-in is not configured.")
        return await fresh_access_token(db, cipher, oauth, user)

    return RepoAccess(request.app.state.redis, _client(request), token_for)


RepoAccessDep = Annotated[RepoAccess, Depends(get_repo_access)]


async def get_viewer(access: OrgAccessDep, readers: RepoAccessDep) -> Credential:
    """The signed-in user's own GitHub credential, for asking GitHub what they can see."""
    try:
        return await readers.credential(access.user)
    except OAuthError as exc:
        raise GitHubReauthRequiredError(
            "Sign in with GitHub again to see this organization's repositories."
        ) from exc


ViewerDep = Annotated[Credential, Depends(get_viewer)]


async def checked(readers: RepoAccess, user: User, repos: list[Repo]) -> list[Repo]:
    """``repos`` the user can read, with GitHub problems as API errors."""
    try:
        return await readers.readable(user, repos)
    except OAuthError as exc:
        raise GitHubReauthRequiredError(
            "Sign in with GitHub again to see this organization's repositories."
        ) from exc
    except GitHubError as exc:
        raise GitHubUnavailableError("GitHub is not responding.") from exc


async def get_repo(
    repo: Annotated[str, Path(description="Repository slug", max_length=201)],
    access: OrgAccessDep,
    db: DbSession,
    repos: RepoServiceDep,
    readers: RepoAccessDep,
) -> Repo:
    """The repository, if it exists in the org and the signed-in user can read it on GitHub.
    Both cases answer 404 alike, so the response doesn't reveal what exists."""
    found = await repos.get(db, access, repo)
    if found is None or not await checked(readers, access.user, [found]):
        raise NotFoundError("Repository not found.", code="repo_not_found_or_no_access")
    structlog.contextvars.bind_contextvars(repo_id=str(found.id))
    return found


RepoDep = Annotated[Repo, Depends(get_repo)]
