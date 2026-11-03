"""What anyone may do (10.1): a day's quotas per person, read-only past the day's global spend
cap, and the public tier's checks on an import (blocked repositories, too many lines)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import LimitsSettings
from sensei_core.errors import ForbiddenError, InvalidError
from sensei_core.limits import (
    QuotaExceededError,
    ReadOnlyError,
    check_global_spend,
    require_writable,
    spent_today_everywhere,
    take_quota,
)
from sensei_core.mail import MemoryMailer
from sensei_core.models import UsageEvent, UsageKind
from sensei_core.orgs.service import OrgService
from sensei_core.repos.service import RepoFacts, estimated_lines, within_public_tier
from sensei_testing import make_user

NOON = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


async def test_a_quota_counts_a_persons_day_and_says_when_it_resets(
    fake_redis: FakeAsyncRedis,
) -> None:
    person, other = uuid.uuid4(), uuid.uuid4()
    for _ in range(2):
        await take_quota(fake_redis, "asks", person, 2, what="questions", now=NOON)
    with pytest.raises(QuotaExceededError) as over:
        await take_quota(fake_redis, "asks", person, 2, what="questions", now=NOON)
    assert over.value.status_code == 429
    assert str(over.value) == "You've reached today's 2 questions. It resets at midnight UTC."
    assert over.value.headers == {"Retry-After": str(12 * 3600)}
    # Someone else's, and the next day's, are their own.
    await take_quota(fake_redis, "asks", other, 2, what="questions", now=NOON)
    tomorrow = NOON.replace(day=4)
    await take_quota(fake_redis, "asks", person, 2, what="questions", now=tomorrow)


async def test_past_the_global_cap_nothing_new_starts_until_midnight(
    system_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> None:
    owner = await make_user(system_session)
    access = await OrgService(MemoryMailer(), "http://localhost:3000").create(
        system_session, owner, name="Spender", slug=f"spender-{uuid.uuid4().hex[:6]}"
    )
    before = await spent_today_everywhere(system_session)
    await require_writable(fake_redis)
    assert not await check_global_spend(system_session, fake_redis, Decimal(0))  # no cap
    system_session.add(UsageEvent(org_id=access.org.id, kind=UsageKind.LLM, provider="anthropic",
                                  model="claude-sonnet-5-5", cost_usd=Decimal("3"),
                                  price_version="test"))  # fmt: skip
    await system_session.flush()
    assert await spent_today_everywhere(system_session) == before + 3
    assert not await check_global_spend(system_session, fake_redis, before + 10)
    await require_writable(fake_redis)
    assert await check_global_spend(system_session, fake_redis, before + 2)
    with pytest.raises(ReadOnlyError) as closed:
        await require_writable(fake_redis)
    assert closed.value.status_code == 503
    assert "reading works" in str(closed.value)


def _facts(full_name: str, code_bytes: int) -> RepoFacts:
    return RepoFacts({"full_name": full_name, "size": 1}, {"Python": code_bytes}, None)


def test_a_public_import_is_refused_blocked_or_too_large() -> None:
    limits = LimitsSettings(public_max_loc=1_000, blocked_repos=["Spam/Repo"])
    within_public_tier(_facts("acme/api", 45 * 1_000), limits)  # at the cap: taken
    assert estimated_lines(_facts("acme/api", 45 * 1_000)) == 1_000
    with pytest.raises(InvalidError) as large:
        within_public_tier(_facts("acme/big", 45 * 1_001), limits)
    assert large.value.code == "too_large_for_public"
    with pytest.raises(ForbiddenError) as blocked:
        within_public_tier(_facts("spam/repo", 10), limits)
    assert blocked.value.code == "repo_blocked"
