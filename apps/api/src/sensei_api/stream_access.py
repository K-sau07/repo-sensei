"""Which of a stream's events, each about a repository, its reader may see: those of repositories
they can read on GitHub (as the repository's own pages answer), each answer trusted for five
minutes. Asked in short sessions of its own: a stream holds no database connection."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request

from sensei_api import streams
from sensei_api.repos import repo_access
from sensei_core.db.tenancy import set_org_context
from sensei_core.github.client import GitHubError
from sensei_core.github.oauth import GitHubOAuth, OAuthError
from sensei_core.models import Repo, User

ACCESS_RECHECK_SECONDS = 300.0  # how long a stream trusts its answer about one repository

RepoCheck = Callable[[str], Awaitable[bool]]


def readable_repos(
    request: Request, oauth: GitHubOAuth | None, org_id: uuid.UUID, user_id: uuid.UUID
) -> RepoCheck:
    """Whether the user may read the repository with this id (as a stream's event names it)."""
    known: dict[str, tuple[bool, float]] = {}

    async def check(repo_id: str) -> bool:
        cached = known.get(repo_id)
        if cached is None or time.monotonic() - cached[1] > ACCESS_RECHECK_SECONDS:
            readable = await _can_read(request, oauth, org_id, user_id, repo_id)
            known[repo_id] = cached = (readable, time.monotonic())
        return cached[0]

    return check


async def _can_read(
    request: Request,
    oauth: GitHubOAuth | None,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    repo_id: str,
) -> bool:
    try:
        target = uuid.UUID(repo_id)
    except ValueError:
        return False
    async with streams.short_session(request) as db:
        await set_org_context(db, org_id)
        repo = await db.get(Repo, target)
        user = await db.get(User, user_id)
        if repo is None or user is None or repo.deleted_at is not None:
            return False
        try:
            return await repo_access(request, db, oauth).can_read(user, repo)
        except (OAuthError, GitHubError):
            return False
