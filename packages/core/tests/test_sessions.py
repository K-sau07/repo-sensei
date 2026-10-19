from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.auth.sessions import SessionService, hash_token
from sensei_core.config import AuthSettings
from sensei_testing import make_user

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(clock: Clock) -> SessionService:
    settings = AuthSettings(
        session_idle_days=14, session_absolute_days=30, session_touch_after_seconds=3600
    )
    return SessionService(settings, clock=clock)


async def test_only_hashes_are_stored(db_session: AsyncSession, service: SessionService) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user, ip="127.0.0.1", user_agent="pytest")
    assert issued.session.token_hash == hash_token(issued.token)
    assert issued.token.encode() not in issued.session.token_hash
    assert service.csrf_matches(issued.session, issued.csrf_token)
    assert not service.csrf_matches(issued.session, "wrong")


async def test_authenticate_returns_user(db_session: AsyncSession, service: SessionService) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    active = await service.authenticate(db_session, issued.token)
    assert active is not None
    assert active.user.id == user.id
    assert await service.authenticate(db_session, "not-a-real-token") is None


async def test_idle_expiry_and_sliding(
    db_session: AsyncSession, service: SessionService, clock: Clock
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)

    clock.advance(days=13)
    assert await service.authenticate(db_session, issued.token) is not None  # slides to day 27

    clock.advance(days=13)
    assert await service.authenticate(db_session, issued.token) is not None  # still idle-valid

    clock.advance(days=15)  # 15 idle days > 14
    assert await service.authenticate(db_session, issued.token) is None


async def test_absolute_expiry_cannot_be_extended(
    db_session: AsyncSession, service: SessionService, clock: Clock
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    for _ in range(4):  # active every 7 days...
        clock.advance(days=7)
        assert await service.authenticate(db_session, issued.token) is not None
    clock.advance(days=3)  # ...but day 31 is past the 30-day absolute lifetime
    assert await service.authenticate(db_session, issued.token) is None


async def test_touch_is_throttled(
    db_session: AsyncSession, service: SessionService, clock: Clock
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    clock.advance(minutes=10)
    active = await service.authenticate(db_session, issued.token)
    assert active is not None
    assert active.session.last_seen_at == T0  # not rewritten within the touch interval
    clock.advance(hours=2)
    active = await service.authenticate(db_session, issued.token)
    assert active is not None
    assert active.session.last_seen_at == clock.now


async def test_revoke_only_own_sessions(db_session: AsyncSession, service: SessionService) -> None:
    alice, bob = await make_user(db_session), await make_user(db_session)
    alice_session = await service.create(db_session, alice)
    assert await service.revoke(db_session, bob.id, alice_session.session.id) is False
    assert await service.authenticate(db_session, alice_session.token) is not None
    assert await service.revoke(db_session, alice.id, alice_session.session.id) is True
    assert await service.authenticate(db_session, alice_session.token) is None


async def test_revoke_all(db_session: AsyncSession, service: SessionService) -> None:
    user = await make_user(db_session)
    tokens = [(await service.create(db_session, user)).token for _ in range(3)]
    assert await service.revoke_all(db_session, user.id) == 3
    for token in tokens:
        assert await service.authenticate(db_session, token) is None
    assert await service.list_active(db_session, user.id) == []


async def test_deleted_users_cannot_authenticate(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    user.deleted_at = T0
    await db_session.flush()
    assert await service.authenticate(db_session, issued.token) is None
