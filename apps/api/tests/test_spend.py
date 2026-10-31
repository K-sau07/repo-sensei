"""Spend holds (4.8): what runs under way set aside, taken down as they spend, so the day's cap
counts a run's spend once (in the ledger) rather than twice (spent, and still held)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from fakeredis import FakeAsyncRedis

from sensei_api.spend import draw, held, hold, release


async def test_a_run_s_spend_comes_off_its_hold() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    org, run, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await hold(redis, org, run, Decimal("0.50"))
    await hold(redis, org, other, Decimal("0.50"))
    await draw(redis, org, run, Decimal("0.45"))
    assert await held(redis, org) == Decimal("0.55")  # 0.05 left of one, 0.50 of the other
    await draw(redis, org, run, Decimal("1"))  # more than it held: none left, never below
    assert await held(redis, org) == Decimal("0.50")
    await draw(redis, org, uuid.uuid4(), Decimal("1"))  # a run that holds nothing: no change
    await release(redis, org, other)
    assert await held(redis, org) == Decimal(0)


async def test_a_draw_after_the_release_does_not_bring_the_hold_back() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    org, run = uuid.uuid4(), uuid.uuid4()
    await hold(redis, org, run, Decimal("0.50"))
    await release(redis, org, run)
    await draw(redis, org, run, Decimal("0.10"))  # a turn's usage, reported late
    assert await held(redis, org) == Decimal(0)


async def test_draws_at_once_each_count() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    org, run = uuid.uuid4(), uuid.uuid4()
    await hold(redis, org, run, Decimal("0.50"))
    await asyncio.gather(*(draw(redis, org, run, Decimal("0.10")) for _ in range(3)))
    assert await held(redis, org) == Decimal("0.20")
