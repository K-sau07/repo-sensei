"""Who may read a repository: we mirror GitHub's own answer instead of keeping our own ACLs.

* A repository imported by public URL: any member of the org.
* A repository connected through the GitHub App: the member's own GitHub token must be able to
  read it (``GET /repositories/{id}``, by id so renames don't matter). The installation can see
  every repository it covers, but a member who can't read one on GitHub must not read it here.

Answers are cached in Redis for five minutes per (user, repository); ``forget_repo`` and
``forget_user`` drop them early (access changes, membership changes).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import cast

from redis.asyncio import Redis

from sensei_core.github.client import Credential, GitHubClient, GitHubError
from sensei_core.models import Repo, RepoSource, User

ACCESS_TTL_SECONDS = 300
_KEY = "access:{user}:{repo}"
_BY_REPO = "access:repo:{repo}"
_BY_USER = "access:user:{user}"

TokenFor = Callable[[User], Awaitable[str]]


class RepoAccess:
    def __init__(
        self,
        redis: Redis,
        client: GitHubClient,
        token_for: TokenFor,
        *,
        ttl: int = ACCESS_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._client = client
        self._token_for = token_for
        self._ttl = ttl

    async def can_read(self, user: User, repo: Repo) -> bool:
        """Whether ``user`` (already a member of the repo's org) may read ``repo``."""
        if repo.source is RepoSource.PUBLIC_URL:
            return True
        key = _KEY.format(user=user.id, repo=repo.id)
        cached = cast("str | None", await self._redis.get(key))
        if cached is not None:
            return cached == "1"
        allowed = await self._ask_github(user, repo)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(key, "1" if allowed else "0", ex=self._ttl)
            for index in (_BY_REPO.format(repo=repo.id), _BY_USER.format(user=user.id)):
                pipe.sadd(index, key)  # pyright: ignore[reportUnknownMemberType]
                pipe.expire(index, self._ttl)
            await pipe.execute()  # pyright: ignore[reportUnknownMemberType]
        return allowed

    async def readable(self, user: User, repos: list[Repo]) -> list[Repo]:
        return [repo for repo in repos if await self.can_read(user, repo)]

    async def forget_repo(self, repo_id: uuid.UUID) -> None:
        await self._forget(_BY_REPO.format(repo=repo_id))

    async def forget_user(self, user_id: uuid.UUID) -> None:
        await self._forget(_BY_USER.format(user=user_id))

    async def _forget(self, index: str) -> None:
        keys = await cast("Awaitable[set[str]]", self._redis.smembers(index))  # pyright: ignore[reportUnknownMemberType]
        await self._redis.delete(index, *keys)

    async def _ask_github(self, user: User, repo: Repo) -> bool:
        token = await self._token_for(user)
        credential = Credential(f"Bearer {token}", f"user:{user.id}")
        try:
            await self._client.get_json(f"/repositories/{repo.github_repo_id}", credential)
        except GitHubError as exc:
            if exc.status in (403, 404):  # GitHub answers 404 for private repos you can't see
                return False
            raise
        return True
