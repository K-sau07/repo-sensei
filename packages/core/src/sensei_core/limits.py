"""What anyone may do (10.1): a day's quotas per person, and the service read-only for the rest of
the day once every org's spend together reaches the day's cap.

- **Quotas** count in Redis, per person, name and UTC day (``quota:<name>:<user>:<day>``): what
  they import by URL, the index runs they start, the questions they ask. Past one, an error says
  when it resets (midnight UTC). Redis being unavailable lets the request through (logged), as
  the rate limits do: a quota isn't worth an outage of what it guards.
- **Read-only**: a worker sums all orgs' LLM spend since midnight UTC every minute
  (:func:`check_global_spend`, with the system database); past the cap it sets a flag that lasts
  the day, and nothing new that costs starts (an ask, a diagram, a fix or change, an index run)
  until it lapses. Reading what's there goes on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.errors import DomainError
from sensei_core.logging import get_logger
from sensei_core.models import UsageEvent

log = get_logger(__name__)

QUOTA = "quota:{name}:{user}:{day}"
READ_ONLY = "limits:read-only:{day}"
READ_ONLY_WAIT_SECONDS = 900  # how long work held back while read-only waits to ask again
# Why an index run's paid stage (embeddings, summaries) was passed over while read-only.
READ_ONLY_SKIP = "the service reached today's spending limit (reindex after midnight UTC to add it)"


class QuotaExceededError(DomainError):
    status_code = 429
    code = "quota_exceeded"


class ReadOnlyError(DomainError):
    status_code = 503
    code = "read_only"


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _wait(now: datetime) -> dict[str, str]:
    return {"Retry-After": str(max(1, int((_midnight(now) - now).total_seconds())))}


async def check_quota(
    redis: Redis,
    name: str,
    user_id: uuid.UUID,
    per_day: int,
    *,
    what: str,
    now: datetime | None = None,
) -> None:
    """:class:`QuotaExceededError` if the person has used the day's ``per_day`` of ``name``
    (``what``: how it's said, "imports"); counts nothing. Check first, then count what went
    through (:func:`count_quota`): a request refused for another reason costs no quota."""
    moment = _now(now)
    key = QUOTA.format(name=name, user=user_id, day=moment.date().isoformat())
    try:
        taken = await redis.get(key)
    except RedisError as exc:
        log.warning("quota unavailable", name=name, error=type(exc).__name__)
        return
    if int(taken or 0) >= per_day:
        _refuse(name, per_day, what, moment)


async def count_quota(
    redis: Redis, name: str, user_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """Count one of ``name`` against the person's day (it went through)."""
    moment = _now(now)
    key = QUOTA.format(name=name, user=user_id, day=moment.date().isoformat())
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, int(timedelta(days=2).total_seconds()))
            await pipe.execute()  # pyright: ignore[reportUnknownMemberType]
    except RedisError as exc:
        log.warning("quota unavailable", name=name, error=type(exc).__name__)


def _refuse(name: str, per_day: int, what: str, moment: datetime) -> None:
    raise QuotaExceededError(
        f"You've reached today's {per_day:,} {what}. It resets at midnight UTC.",
        details={"quota": name, "per_day": per_day, "resets_at": _midnight(moment).isoformat()},
        headers=_wait(moment),
    )


async def take_quota(
    redis: Redis,
    name: str,
    user_id: uuid.UUID,
    per_day: int,
    *,
    what: str,
    now: datetime | None = None,
) -> None:
    """Count one of ``name`` against the person's day, refusing past ``per_day``
    (:class:`QuotaExceededError`): for what can't be checked first."""
    moment = _now(now)
    key = QUOTA.format(name=name, user=user_id, day=moment.date().isoformat())
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, int(timedelta(days=2).total_seconds()))
            taken, _ = await pipe.execute()  # pyright: ignore[reportUnknownMemberType]
    except RedisError as exc:
        log.warning("quota unavailable", name=name, error=type(exc).__name__)
        return
    if int(taken) > per_day:
        _refuse(name, per_day, what, moment)


async def require_writable(redis: Redis, *, now: datetime | None = None) -> None:
    """:class:`ReadOnlyError` while the day's spend cap is reached."""
    moment = _now(now)
    try:
        reached = await redis.exists(READ_ONLY.format(day=moment.date().isoformat()))
    except RedisError as exc:
        log.warning("read-only flag unavailable", error=type(exc).__name__)
        return
    if reached:
        raise ReadOnlyError(
            "repo-sensei has reached today's spending limit: reading works, but nothing new "
            "that costs can start until midnight UTC.",
            details={"resets_at": _midnight(moment).isoformat()},
            headers=_wait(moment),
        )


async def read_only(redis: Redis, *, now: datetime | None = None) -> bool:
    """Whether the day's spend cap is reached (work that would cost waits)."""
    try:
        await require_writable(redis, now=now)
    except ReadOnlyError:
        return True
    return False


async def spent_today_everywhere(db: AsyncSession, now: datetime | None = None) -> Decimal:
    """Every org's LLM spend since midnight UTC (``db``: the system database's, reading all)."""
    start = _midnight(_now(now)) - timedelta(days=1)
    total = await db.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.created_at >= start
        )
    )
    return Decimal(total or 0)


async def check_global_spend(
    db: AsyncSession, redis: Redis, cap: Decimal, *, now: datetime | None = None
) -> bool:
    """Set the day's read-only flag once every org's spend reaches ``cap`` (0: no cap); whether
    the service is read-only now."""
    if cap <= 0:
        return False
    moment = _now(now)
    spent = await spent_today_everywhere(db, moment)
    if spent < cap:
        return False
    # Until the day ends (and an hour more, so a clock a little behind still sees it).
    lasts = int((_midnight(moment) - moment).total_seconds()) + 3600
    first = await redis.set(READ_ONLY.format(day=moment.date().isoformat()), str(spent),
                            ex=lasts, nx=True)  # fmt: skip
    if first:
        log.warning("global spend cap reached: read-only until midnight UTC",
                    spent_usd=str(spent), cap_usd=str(cap))  # fmt: skip
    return True
