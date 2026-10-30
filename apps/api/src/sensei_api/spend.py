"""What an org may still spend on agents today (4.8): its LLM spend since UTC midnight (the
usage ledger's), against the daily cap. Past the cap no new run starts; at its warning share
the org is told, once a day (an audit event until there is an inbox)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.config import AgentSettings
from sensei_core.errors import DomainError
from sensei_core.models import UsageEvent

WARNED_PREFIX = "agent:cap-warned:"


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
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> Decimal | None:
    """What the org may still spend today (None: no cap); past the cap, an error saying when
    it resets. At the warning share, the org is told once a day."""
    cap = settings.daily_cost_cap_usd
    if cap <= 0:
        return None
    moment = now or datetime.now(UTC)
    spent = await spent_today(db, org_id, moment)
    resets = day_start(moment) + timedelta(days=1)
    if spent >= cap:
        raise CostCapReachedError(
            "This organization has used its agent budget for today.",
            details={"resets_at": resets.isoformat(), "cap_usd": str(cap)},
            headers={"Retry-After": str(max(1, int((resets - moment).total_seconds())))},
        )
    if spent >= cap * Decimal(str(settings.cap_warning_share)):
        day = day_start(moment)
        await _warn(db, redis, org_id=org_id, user_id=user_id, spent=spent, cap=cap, day=day)
    return cap - spent


async def _warn(
    db: AsyncSession,
    redis: Redis,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    spent: Decimal,
    cap: Decimal,
    day: datetime,
) -> None:
    key = f"{WARNED_PREFIX}{org_id}:{day.date().isoformat()}"
    try:
        first = await redis.set(key, "1", nx=True, ex=int(timedelta(days=1).total_seconds()))
    except RedisError:
        return  # unsure whether it was said: say nothing rather than every time
    if first:
        await audit(
            db,
            AuditAction.AGENT_COST_CAP_WARNING,
            actor_user_id=user_id,
            org_id=org_id,
            target_type="org",
            target_id=org_id,
            meta={"spent_usd": str(spent), "cap_usd": str(cap)},
        )
