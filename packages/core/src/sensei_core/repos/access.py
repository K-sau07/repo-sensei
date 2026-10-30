"""Who may read a repository: we mirror GitHub's own answer instead of keeping our own ACLs.

* A repository imported by public URL: any member of the org, while it stays public. Once it is
  found gone or private (``access_lost``), only members whose own GitHub token can read it.
* A repository connected through the GitHub App: the member's own GitHub token must be able to
  read it (``GET /repositories/{id}``, by id so renames don't matter). The installation can see
  every repository it covers, but a member who can't read one on GitHub must not read it here.

Answers are cached in Redis for five minutes per (user, repository). ``forget_repo`` and
``forget_user`` drop them early: they bump a generation number that is part of every cache key,
so an answer GitHub gave before the bump (even one written after it) is never read again.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import cast

from redis.asyncio import Redis

from sensei_core.github.client import Credential, GitHubClient, GitHubError
from sensei_core.github.oauth import OAuthError
from sensei_core.models import Repo, RepoSource, RepoStatus, User

ACCESS_TTL_SECONDS = 300
CONCURRENT_CHECKS = 8  # GitHub lookups in flight per listing
_KEY = "access:{user}:{repo}:{user_gen}.{repo_gen}"
_REPO_GEN = "access:gen:repo:{repo}"
_USER_GEN = "access:gen:user:{user}"

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

    async def credential(self, user: User) -> Credential:
        """The user's own GitHub credential. Raises :class:`OAuthError` if they must sign in
        with GitHub again."""
        token = await self._token_for(user)
        return Credential(f"Bearer {token}", f"user:{user.id}")

    async def can_read(self, user: User, repo: Repo) -> bool:
        """Whether ``user`` (already a member of the repo's org) may read ``repo``."""
        return (await self.readable(user, [repo])) == [repo]

    async def readable(self, user: User, repos: list[Repo]) -> list[Repo]:
        """The ``repos`` the user may read, in order. Uncached ones are asked a few at a time."""
        answers: dict[uuid.UUID, bool] = {}
        unknown: list[tuple[Repo, str]] = []
        for repo in repos:
            if open_to_all(repo):
                answers[repo.id] = True
                continue
            key = await self._key(user.id, repo.id)
            cached = cast("str | None", await self._redis.get(key))
            if cached is None:
                unknown.append((repo, key))
            else:
                answers[repo.id] = cached == "1"
        if unknown:
            credential = await self.credential(user)  # once: the session isn't shared by tasks
            limit = asyncio.Semaphore(CONCURRENT_CHECKS)

            async def check(repo: Repo, key: str) -> None:
                async with limit:
                    allowed = await self._ask_github(credential, repo)
                # Under the generations read before asking: a forget in between makes this
                # answer unreachable instead of reviving it.
                await self._redis.set(key, "1" if allowed else "0", ex=self._ttl)
                answers[repo.id] = allowed

            results = await asyncio.gather(
                *(check(repo, key) for repo, key in unknown), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result  # every check has finished: nothing left running
        return [repo for repo in repos if answers[repo.id]]

    async def forget_repo(self, repo_id: uuid.UUID) -> None:
        await self._bump(_REPO_GEN.format(repo=repo_id))

    async def forget_user(self, user_id: uuid.UUID) -> None:
        await self._bump(_USER_GEN.format(user=user_id))

    async def _bump(self, key: str) -> None:
        # Kept longer than any answer, so a generation never falls back to one still cached.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self._ttl * 2)
            await pipe.execute()  # pyright: ignore[reportUnknownMemberType]

    async def _key(self, user_id: uuid.UUID, repo_id: uuid.UUID) -> str:
        user_gen, repo_gen = cast(
            "list[str | None]",
            await self._redis.mget(_USER_GEN.format(user=user_id), _REPO_GEN.format(repo=repo_id)),
        )
        return _KEY.format(
            user=user_id, repo=repo_id, user_gen=user_gen or "0", repo_gen=repo_gen or "0"
        )

    async def _ask_github(self, credential: Credential, repo: Repo) -> bool:
        try:
            await self._client.get_json(f"/repositories/{repo.github_repo_id}", credential)
        except GitHubError as exc:
            if exc.status in (403, 404):  # GitHub answers 404 for private repos you can't see
                return False
            if exc.status == 401:  # the user's token was revoked or expired for good
                raise OAuthError("github_reauth_required", "Sign in with GitHub again.") from None
            raise
        return True


def open_to_all(repo: Repo) -> bool:
    """A public repository imported by its URL: anyone may read it, signed in or not."""
    return repo.source is RepoSource.PUBLIC_URL and repo.status is not RepoStatus.ACCESS_LOST
