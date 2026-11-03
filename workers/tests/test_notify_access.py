"""Whether an email's person may read its repository (8.10): asked of GitHub with their own
token, as the API asks; can't be told when their sign-in lapsed, nor without GitHub sign-in."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
import respx
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.auth.github_login import store_tokens
from sensei_core.config import GitHubSettings, Settings
from sensei_core.github.oauth import GitHubTokens
from sensei_core.models import Repo, RepoSource
from sensei_testing import make_user
from sensei_workers.runtime import Runtime
from sensei_workers.tasks.notifications import can_read

pytestmark = pytest.mark.integration

GITHUB = "https://api.github.com"


def _repo(github_id: int) -> Repo:
    return Repo(id=uuid.uuid4(), org_id=uuid.uuid4(), source=RepoSource.GITHUB_APP,
                github_repo_id=github_id, owner="o", name="r", full_name="o/r", slug="r",
                default_branch="main", visibility="private")  # fmt: skip


async def test_github_is_asked_with_the_persons_own_token(
    migrated_test_db: Settings, db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    github = GitHubSettings(client_id="Iv23.test", client_secret=SecretStr("shh"))
    runtime = Runtime(migrated_test_db.model_copy(update={"github": github}))
    runtime._redis = fake_redis  # pyright: ignore[reportPrivateUsage] - no real Redis here
    user, lapsed = await make_user(db_session), await make_user(db_session)
    tokens = GitHubTokens("ghu_person", None, None, None, [])
    await store_tokens(db_session, runtime.cipher, user, tokens)

    @asynccontextmanager
    async def same() -> AsyncGenerator[AsyncSession]:
        yield db_session

    check = can_read(runtime, same)
    assert check is not None
    with respx.mock() as mock:
        asked = mock.get(f"{GITHUB}/repositories/1").respond(200, json={"id": 1})
        mock.get(f"{GITHUB}/repositories/2").respond(404, json={"message": "Not Found"})
        assert await check(user, _repo(1)) is True
        assert await check(user, _repo(2)) is False
        assert asked.calls.last.request.headers["authorization"] == "Bearer ghu_person"
        assert await check(lapsed, _repo(1)) is None  # no token: they must sign in again
    http = runtime._http  # pyright: ignore[reportPrivateUsage] - the GitHub client's, made above
    if http is not None:
        await http.aclose()


def test_without_github_sign_in_nothing_can_be_asked() -> None:
    runtime = Runtime(Settings(env="test"))

    @asynccontextmanager
    async def never() -> AsyncGenerator[AsyncSession]:
        raise AssertionError("no session is needed")
        yield  # pragma: no cover

    assert can_read(runtime, never) is None
