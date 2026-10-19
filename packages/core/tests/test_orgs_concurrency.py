"""Concurrent membership changes, over real committed data and two app-role connections."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from sensei_core.config import Settings
from sensei_core.errors import ConflictError
from sensei_core.mail import MemoryMailer
from sensei_core.models import Membership, Org, Role, User
from sensei_core.orgs.service import OrgService

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class TwoOwners:
    org_slug: str
    first: uuid.UUID
    second: uuid.UUID


@pytest.fixture
async def app_engine(migrated_test_db: Settings) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(migrated_test_db.database.url.get_secret_value())
    yield engine
    await engine.dispose()


@pytest.fixture
async def two_owners(migrated_test_db: Settings) -> AsyncGenerator[TwoOwners]:
    """An org with two owners, committed by the system role and removed afterwards."""
    system = create_async_engine(migrated_test_db.database.system_url.get_secret_value())
    suffix = uuid.uuid4().hex[:8]
    org_id, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with system.begin() as conn:
        await conn.execute(
            insert(User),
            [
                {"id": first, "github_id": int(suffix, 16), "login": f"a{suffix}"},
                {"id": second, "github_id": int(suffix, 16) + 1, "login": f"b{suffix}"},
            ],
        )
        await conn.execute(insert(Org).values(id=org_id, slug=f"race-{suffix}", name="Race"))
        await conn.execute(
            insert(Membership),
            [
                {"org_id": org_id, "user_id": first, "role": Role.OWNER},
                {"org_id": org_id, "user_id": second, "role": Role.OWNER},
            ],
        )
    try:
        yield TwoOwners(org_slug=f"race-{suffix}", first=first, second=second)
    finally:
        async with system.begin() as conn:
            await conn.execute(delete(Org).where(Org.id == org_id))
            await conn.execute(delete(User).where(User.id.in_([first, second])))
        await system.dispose()


async def _leave(engine: AsyncEngine, slug: str, user_id: uuid.UUID, hold: asyncio.Event) -> None:
    service = OrgService(MemoryMailer(), "https://app.example.com")
    async with AsyncSession(engine, expire_on_commit=False) as db, db.begin():
        user = await db.get(User, user_id)
        assert user is not None
        access = await service.access(db, user, slug)
        await service.leave(db, access)
        await hold.wait()  # keep the transaction (and its lock) open until told to commit


async def test_two_owners_cannot_both_leave(app_engine: AsyncEngine, two_owners: TwoOwners) -> None:
    first_may_commit, second_may_commit = asyncio.Event(), asyncio.Event()
    second_may_commit.set()
    slug = two_owners.org_slug
    first = asyncio.create_task(_leave(app_engine, slug, two_owners.first, first_may_commit))
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.sleep(0.3)  # the first owner has left but not committed yet
        second = asyncio.create_task(_leave(app_engine, slug, two_owners.second, second_may_commit))
        await asyncio.sleep(0.3)
        assert not second.done(), "the second owner must wait for the first transaction"

        first_may_commit.set()
        await asyncio.wait_for(first, timeout=10)
        with pytest.raises(ConflictError) as exc:
            await asyncio.wait_for(second, timeout=10)
        assert exc.value.code == "last_owner"
    finally:
        # Never leave a transaction open: it would block the fixture's cleanup forever.
        first_may_commit.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
