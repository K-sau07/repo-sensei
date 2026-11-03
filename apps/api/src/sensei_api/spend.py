"""What an org may still spend on agents today (4.8): its LLM spend since UTC midnight (the
usage ledger's), against the daily cap, less what the runs under way may still spend (each
holds its budget while it runs, so runs at once can't each spend what is left). Past the cap
no new run starts; at its warning share the org is told, once a day: an audit event, and its
admins' inboxes (and email, and Slack: 8.10), in a transaction of its own: a request that
fails after it doesn't take the warning back with it."""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.config import AgentSettings
from sensei_core.db.tenancy import set_org_context
from sensei_core.errors import DomainError
from sensei_core.logging import get_logger
from sensei_core.models import NotificationKind, UsageEvent
from sensei_core.notifications.service import Event, admins, notify, publish

log = get_logger(__name__)

WARNED_PREFIX = "agent:cap-warned:"
HOLDS_PREFIX = "agent:holds:"  # a hash per org: run id -> "<amount> <unix time it lapses>"
HOLD_SECONDS = 900  # a run's hold lapses after this, should the run never release it

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class CostCapReachedError(DomainError):
    status_code = 429
    code = "cost_cap_reached"


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def spent_today(db: AsyncSession, org_id: uuid.UUID, now: datetime) -> Decimal:
    total = await db.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.org_id == org_id, UsageEvent.created_at >= day_start(now)
        )
    )
    return Decimal(total or 0)


async def remaining_today(
    db: AsyncSession,
    redis: Redis,
    settings: AgentSettings,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    warn_in: Sessions,
    now: datetime | None = None,
) -> Decimal | None:
    """What the org may still spend today (None: no cap); past the cap, an error saying when
    it resets (``user_id``: who is asking, None for someone not signed in). At the warning
    share, the org is told once a day (in a session of ``warn_in``'s, committed then)."""
    cap = settings.daily_cost_cap_usd
    if cap <= 0:
        return None
    moment = now or datetime.now(UTC)
    spent = await spent_today(db, org_id, moment) + await held(redis, org_id)
    resets = day_start(moment) + timedelta(days=1)
    if spent >= cap:
        raise CostCapReachedError(
            "This organization has used its agent budget for today.",
            details={"resets_at": resets.isoformat(), "cap_usd": str(cap)},
            headers={"Retry-After": str(max(1, int((resets - moment).total_seconds())))},
        )
    if spent >= cap * Decimal(str(settings.cap_warning_share)):
        day = day_start(moment)
        await _warn(warn_in, redis, org_id=org_id, user_id=user_id, spent=spent, cap=cap, day=day)
    return cap - spent


async def held(redis: Redis, org_id: uuid.UUID) -> Decimal:
    """What the org's runs under way may still spend (lapsed holds forgotten). Unknown (Redis
    down): nothing, as the rate limit fails open too."""
    key = f"{HOLDS_PREFIX}{org_id}"
    try:
        holds = await cast("Awaitable[dict[bytes | str, bytes | str]]", redis.hgetall(key))  # pyright: ignore[reportUnknownMemberType]
        now = time.time()
        total = Decimal(0)
        lapsed: list[str] = []
        for run, value in holds.items():
            amount, until = _text(value).split()
            if float(until) <= now:
                lapsed.append(_text(run))
            else:
                total += Decimal(amount)
        if lapsed:
            await cast("Awaitable[int]", redis.hdel(key, *lapsed))
    except (RedisError, ValueError):
        return Decimal(0)
    return total


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def hold(redis: Redis, org_id: uuid.UUID, run_id: uuid.UUID, amount: Decimal) -> None:
    """Set ``amount`` aside for a run until it ends (:func:`release`) or the hold lapses."""
    key = f"{HOLDS_PREFIX}{org_id}"
    try:
        until = time.time() + HOLD_SECONDS
        await cast("Awaitable[int]", redis.hset(key, str(run_id), f"{amount} {until}"))  # pyright: ignore[reportUnknownMemberType]
        await redis.expire(key, HOLD_SECONDS)
    except RedisError:
        return


DRAW_TRIES = 5  # a draw raced by another change to the org's holds is tried again, this often


async def draw(redis: Redis, org_id: uuid.UUID, run_id: uuid.UUID, cost: Decimal) -> None:
    """Take what a run just spent (now in the ledger) off its hold, so the day's cap doesn't
    count it twice: once spent, once still held. In a transaction that watches the org's
    holds: two draws at once both count, and one after the hold's release doesn't bring it
    back."""
    key = f"{HOLDS_PREFIX}{org_id}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            for _ in range(DRAW_TRIES):
                try:
                    await pipe.watch(key)
                    value = await cast("Awaitable[bytes | str | None]", pipe.hget(key, str(run_id)))  # pyright: ignore[reportUnknownMemberType]
                    if value is None:
                        await pipe.unwatch()
                        return
                    amount, until = _text(value).split()
                    left = max(Decimal(0), Decimal(amount) - cost)
                    pipe.multi()
                    pipe.hset(key, str(run_id), f"{left} {until}")  # pyright: ignore[reportUnknownMemberType]
                    await pipe.execute()
                    return
                except WatchError:
                    continue
        # Raced every time: the hold stays as it was, over-counting until it lapses.
        log.warning("spend not taken off its hold", run_id=str(run_id))
    except (RedisError, ValueError, ArithmeticError):
        return


async def release(redis: Redis, org_id: uuid.UUID, run_id: uuid.UUID) -> None:
    try:
        await cast("Awaitable[int]", redis.hdel(f"{HOLDS_PREFIX}{org_id}", str(run_id)))
    except RedisError:
        return


async def _warn(
    sessions: Sessions,
    redis: Redis,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    spent: Decimal,
    cap: Decimal,
    day: datetime,
) -> None:
    key = f"{WARNED_PREFIX}{org_id}:{day.date().isoformat()}"
    try:
        first = await redis.set(key, "1", nx=True, ex=int(timedelta(days=1).total_seconds()))
    except RedisError:
        return  # unsure whether it was said: say nothing rather than every time
    if not first:
        return
    share = round(spent / cap * 100) if cap else 0
    event = Event(
        org_id, NotificationKind.COST_CAP, f"cost-cap:{org_id}:{day.date().isoformat()}",
        f"Agent spend is at {share}% of today's cap",
        f"${spent:.2f} of ${cap:.2f} so far today (UTC). Past the cap, no new agent run starts "
        "until midnight UTC.",
        None,
    )  # fmt: skip
    try:
        async with sessions() as db:
            await set_org_context(db, org_id)
            await audit(
                db,
                AuditAction.AGENT_COST_CAP_WARNING,
                actor_user_id=user_id,
                org_id=org_id,
                target_type="org",
                target_id=org_id,
                meta={"spent_usd": str(spent), "cap_usd": str(cap)},
            )
            told = await notify(db, event, await admins(db, org_id))
    except Exception as exc:  # telling is worth no request: the next one tries again
        log.warning("cost cap warning not written", error=type(exc).__name__)
        with contextlib.suppress(RedisError):
            await redis.delete(key)
        return
    await publish(redis, [told])  # after the commit: what the inboxes are told is there
