"""An org's usage (10.7.2): what it has spent, against its caps, and the limits each person
works within (admins).

The caps and limits are the deployment's (settings), the same for every org: today's spend
(everything metered, indexing too; UTC day) against ``SENSEI_AGENT_DAILY_COST_CAP_USD``, at
which asks, diagrams, fixes and changes stop starting, this month's
spend against ``SENSEI_PRICING_ORG_MONTHLY_CAP_USD`` (a summaries run that would pass it is
skipped; 0 allows none), and each
person's quotas and rates. The spend is the usage ledger's: the last 30 days by day, by model
and by person.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import Date, cast, func, select

from sensei_api.deps import DbSession, RedisClient
from sensei_api.orgs import OrgAccessDep
from sensei_api.spend import day_start, held, spent_today
from sensei_core.config import Settings
from sensei_core.models import Role, UsageEvent, UsageKind, User
from sensei_core.orgs.service import require_role
from sensei_core.usage import month_start, spent_since

router = APIRouter(prefix="/orgs/{org}", tags=["usage"])

DAYS = 30
TOP_MODELS = 20
TOP_PEOPLE = 10


class TodayOut(BaseModel):
    spent_usd: Decimal = Field(description="Everything metered today, indexing included.")
    held_usd: Decimal = Field(description="Set aside for runs under way.")
    cap_usd: Decimal | None = Field(
        description="The day's cap, at which agent runs stop starting (None: no cap)."
    )
    warn_share: float = Field(description="The share of the cap at which admins are told.")
    resets_at: datetime


class MonthOut(BaseModel):
    spent_usd: Decimal
    cap_usd: Decimal = Field(
        description="The month's budget: a summaries run that would pass it isn't started."
    )
    resets_at: datetime


class DayOut(BaseModel):
    day: date
    cost_usd: Decimal
    by_kind: dict[UsageKind, Decimal]


class ModelOut(BaseModel):
    provider: str
    model: str
    kind: UsageKind
    calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal


class PersonOut(BaseModel):
    login: str | None = Field(description="None: no one's (index runs, a public page's asks).")
    cost_usd: Decimal


class LimitsOut(BaseModel):
    asks_per_minute: int
    asks_per_day: int
    diagrams_per_minute: int
    index_runs_per_day: int
    public_imports_per_day: int


class UsageOut(BaseModel):
    today: TodayOut
    month: MonthOut
    days: list[DayOut] = Field(description="The last 30 days (UTC), oldest first, each one.")
    models: list[ModelOut] = Field(description="The last 30 days, the costliest first.")
    people: list[PersonOut] = Field(description="The last 30 days, the costliest first.")
    limits: LimitsOut


@router.get("/usage", response_model=UsageOut)
async def usage(
    *, request: Request, access: OrgAccessDep, db: DbSession, redis: RedisClient
) -> UsageOut:
    """The org's spend against its caps, and the limits each person works within (admins)."""
    require_role(access, Role.ADMIN)
    settings: Settings = request.app.state.settings
    org_id = access.org.id
    now = datetime.now(UTC)
    first = day_start(now) - timedelta(days=DAYS - 1)
    recent = (UsageEvent.org_id == org_id, UsageEvent.created_at >= first)

    day = cast(func.timezone("UTC", UsageEvent.created_at), Date)
    by_day: dict[date, dict[UsageKind, Decimal]] = {}
    for when, kind, cost in await db.execute(
        select(day, UsageEvent.kind, func.sum(UsageEvent.cost_usd))
        .where(*recent)
        .group_by(day, UsageEvent.kind)
    ):
        by_day.setdefault(when, {})[kind] = Decimal(cost or 0)
    days = [
        DayOut(
            day=d, cost_usd=sum(by_day.get(d, {}).values(), Decimal(0)), by_kind=by_day.get(d, {})
        )
        for d in ((first + timedelta(days=n)).date() for n in range(DAYS))
    ]

    cost = func.sum(UsageEvent.cost_usd)
    models = [
        ModelOut(
            provider=provider,
            model=model,
            kind=kind,
            calls=calls,
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            cost_usd=Decimal(total or 0),
        )
        for provider, model, kind, calls, tokens_in, tokens_out, total in await db.execute(
            select(
                UsageEvent.provider,
                UsageEvent.model,
                UsageEvent.kind,
                func.count(),
                func.sum(UsageEvent.tokens_in),
                func.sum(UsageEvent.tokens_out),
                cost,
            )
            .where(*recent)
            .group_by(UsageEvent.provider, UsageEvent.model, UsageEvent.kind)
            .order_by(cost.desc())
            .limit(TOP_MODELS)
        )
    ]
    people = [
        PersonOut(login=login, cost_usd=Decimal(total or 0))
        for login, total in await db.execute(
            select(User.login, cost)
            .select_from(UsageEvent)
            .outerjoin(User, User.id == UsageEvent.user_id)
            .where(*recent)
            .group_by(User.login)
            .order_by(cost.desc())
            .limit(TOP_PEOPLE)
        )
    ]

    agent = settings.agent
    daily = agent.daily_cost_cap_usd
    monthly = settings.pricing.org_monthly_cap_usd
    month = month_start(now)
    next_month = (month + timedelta(days=32)).replace(day=1)
    return UsageOut(
        today=TodayOut(
            spent_usd=await spent_today(db, org_id, now),
            held_usd=await held(redis, org_id),
            cap_usd=daily if daily > 0 else None,
            warn_share=agent.cap_warning_share,
            resets_at=day_start(now) + timedelta(days=1),
        ),
        month=MonthOut(
            spent_usd=await spent_since(db, org_id, month),
            cap_usd=monthly,
            resets_at=next_month,
        ),
        days=days,
        models=models,
        people=people,
        limits=LimitsOut(
            asks_per_minute=agent.ask_per_minute,
            asks_per_day=settings.limits.asks_per_day,
            diagrams_per_minute=agent.diagrams_per_minute,
            index_runs_per_day=settings.limits.index_runs_per_day,
            public_imports_per_day=settings.limits.public_imports_per_day,
        ),
    )
