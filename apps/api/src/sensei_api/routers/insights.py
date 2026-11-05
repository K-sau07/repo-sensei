"""An org's insights (10.6.1): how it uses repo-sensei over the last 30 days (UTC).

Questions by day and by repository (each first question and follow-up, answered afresh or from
the cache), how the answers went (answered, abstained, and what people said of them), the
modules answers cite most, and each week's adoption (people asking, fixes, changes, diagrams).
Everyone in the org reads it, counted over the repositories they can read on GitHub (as their
home lists them); what it cost is shown to admins only, as on the usage page.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import Date, String, cast, column, distinct, func, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoAccessDep, RepoServiceDep, checked
from sensei_api.spend import day_start
from sensei_core.models import (
    AgentRun,
    Conversation,
    ConversationMessage,
    Diagram,
    FixKind,
    FixRun,
    MessageFeedback,
    Role,
)

router = APIRouter(prefix="/orgs/{org}", tags=["insights"])

DAYS = 30
TOP_REPOS = 10
TOP_MODULES = 40
MODULE_DEPTH = 2  # a module is a file's first two folders (or fewer, for a shallow file)


class InsightDayOut(BaseModel):
    day: date
    questions: int
    people: int = Field(description="Signed-in people who asked that day.")
    cost_usd: Decimal | None = Field(description="Asks' spend (admins only; else None).")


class InsightRepoOut(BaseModel):
    repo_id: uuid.UUID
    full_name: str
    slug: str
    questions: int
    cost_usd: Decimal | None


class InsightAnswersOut(BaseModel):
    answered: int
    abstained: int = Field(description="Said it couldn't answer from the code.")
    helpful: int
    not_helpful: dict[str, int] = Field(description="What was wrong, by kind.")


class InsightWeekOut(BaseModel):
    week: date = Field(description="The week's Monday (UTC).")
    answered: int
    abstained: int
    helpful: int
    not_helpful: int
    askers: int
    fixes: int
    changes: int
    diagrams: int


class InsightModuleOut(BaseModel):
    repo_id: uuid.UUID
    full_name: str
    path: str = Field(description="The folder, '' for files at the root.")
    cited: int = Field(description="Answers citing a file in it.")


class InsightsOut(BaseModel):
    days: list[InsightDayOut] = Field(description="The last 30 days (UTC), oldest first.")
    repos: list[InsightRepoOut] = Field(description="The most asked about first.")
    answers: InsightAnswersOut
    weeks: list[InsightWeekOut] = Field(description="Each week the 30 days touch, oldest first.")
    modules: list[InsightModuleOut] = Field(description="The most cited first.")
    costs: bool = Field(description="Whether the costs are shown (admins).")


def module_of(path: str) -> str:
    return "/".join(path.split("/")[:-1][:MODULE_DEPTH])


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _utc_day(column: Any) -> Any:
    return cast(func.timezone("UTC", column), Date)


_NO_ANSWERS = {"answered": 0, "abstained": 0, "helpful": 0, "not_helpful": 0}
_NO_USE = {"askers": 0, "fixes": 0, "changes": 0, "diagrams": 0}


@router.get("/insights", response_model=InsightsOut)
async def insights(
    *, access: OrgAccessDep, db: DbSession, repos: RepoServiceDep, readers: RepoAccessDep
) -> InsightsOut:
    """The last 30 days of questions, answers, cited modules and adoption, in the repositories
    the signed-in user can read."""
    org_id = access.org.id
    visible = await checked(readers, access.user, await repos.list(db, access))
    names = {repo.id: repo for repo in visible}
    seen = list(names)
    costs = access.role.at_least(Role.ADMIN)
    first = day_start(datetime.now(UTC)) - timedelta(days=DAYS - 1)
    days = [first.date() + timedelta(days=n) for n in range(DAYS)]

    asked = (
        select(ConversationMessage.created_at, Conversation.repo_id, Conversation.user_id)
        .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
        .where(
            ConversationMessage.org_id == org_id,
            ConversationMessage.role == "user",
            ConversationMessage.created_at >= first,
            Conversation.repo_id.in_(seen),
        )
        .subquery()
    )
    asked_day = _utc_day(asked.c.created_at)
    questions: dict[date, tuple[int, int]] = {}
    for day, count, people in await db.execute(
        select(asked_day, func.count(), func.count(distinct(asked.c.user_id))).group_by(asked_day)
    ):
        questions[day] = (count, people)
    by_repo = Counter[uuid.UUID]()
    for repo_id, count in await db.execute(
        select(asked.c.repo_id, func.count()).group_by(asked.c.repo_id)
    ):
        by_repo[repo_id] = count

    spent: dict[date, Decimal] = {}
    repo_cost: dict[uuid.UUID, Decimal] = {}
    if costs:
        asks = (AgentRun.org_id == org_id, AgentRun.kind == "ask", AgentRun.created_at >= first,
                AgentRun.repo_id.in_(seen))  # fmt: skip
        run_day = _utc_day(AgentRun.created_at)
        for day, cost in await db.execute(
            select(run_day, func.sum(AgentRun.cost_usd)).where(*asks).group_by(run_day)
        ):
            spent[day] = cost or Decimal(0)
        for repo_id, cost in await db.execute(
            select(AgentRun.repo_id, func.sum(AgentRun.cost_usd))
            .where(*asks)
            .group_by(AgentRun.repo_id)
        ):
            repo_cost[repo_id] = cost or Decimal(0)

    answers = await _answers(db, org_id, first, seen)
    adoption = await _adoption(db, org_id, first, seen, asked)
    modules = await _modules(db, org_id, first, seen)
    return InsightsOut(
        days=[
            InsightDayOut(
                day=day,
                questions=questions.get(day, (0, 0))[0],
                people=questions.get(day, (0, 0))[1],
                cost_usd=spent.get(day, Decimal(0)) if costs else None,
            )
            for day in days
        ],
        repos=[
            InsightRepoOut(
                repo_id=repo_id,
                full_name=names[repo_id].full_name,
                slug=names[repo_id].slug,
                questions=count,
                cost_usd=repo_cost.get(repo_id, Decimal(0)) if costs else None,
            )
            for repo_id, count in by_repo.most_common(TOP_REPOS)
        ],
        answers=InsightAnswersOut(
            answered=sum(week["answered"] for week in answers.values()),
            abstained=sum(week["abstained"] for week in answers.values()),
            helpful=sum(week["helpful"] for week in answers.values()),
            not_helpful=dict(await _wrong(db, org_id, first, seen)),
        ),
        weeks=[
            InsightWeekOut(
                week=week, **answers.get(week, _NO_ANSWERS), **adoption.get(week, _NO_USE)
            )
            for week in sorted({_monday(day) for day in days})
        ],
        modules=[
            InsightModuleOut(
                repo_id=repo_id, full_name=names[repo_id].full_name, path=path, cited=count
            )
            for (repo_id, path), count in modules.most_common(TOP_MODULES)
        ],
        costs=costs,
    )


def _answered(org_id: uuid.UUID, first: datetime, seen: list[uuid.UUID]) -> Any:
    """The window's answers in the repositories seen."""
    return (
        select(
            ConversationMessage.id,
            ConversationMessage.created_at,
            ConversationMessage.abstained,
            ConversationMessage.status,
        )
        .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
        .where(
            ConversationMessage.org_id == org_id,
            ConversationMessage.role == "assistant",
            ConversationMessage.created_at >= first,
            Conversation.repo_id.in_(seen),
        )
        .subquery()
    )


async def _answers(
    db: AsyncSession, org_id: uuid.UUID, first: datetime, seen: list[uuid.UUID]
) -> dict[date, dict[str, int]]:
    """Each week's finished answers (answered or abstained), and the feedback on its answers."""
    found: dict[date, dict[str, int]] = {}
    answer = _answered(org_id, first, seen)
    day = _utc_day(answer.c.created_at)
    for when, abstained, count in await db.execute(
        select(day, answer.c.abstained, func.count())
        .where(answer.c.status == "done")
        .group_by(day, answer.c.abstained)
    ):
        week = found.setdefault(_monday(when), dict(_NO_ANSWERS))
        week["abstained" if abstained else "answered"] += count
    for when, kind, count in await db.execute(
        select(day, MessageFeedback.kind, func.count())
        .join(answer, answer.c.id == MessageFeedback.message_id)
        .where(MessageFeedback.org_id == org_id)
        .group_by(day, MessageFeedback.kind)
    ):
        week = found.setdefault(_monday(when), dict(_NO_ANSWERS))
        week["helpful" if kind == "helpful" else "not_helpful"] += count
    return found


async def _wrong(
    db: AsyncSession, org_id: uuid.UUID, first: datetime, seen: list[uuid.UUID]
) -> Counter[str]:
    """What people said was wrong with the window's answers, by kind."""
    wrong = Counter[str]()
    answer = _answered(org_id, first, seen)
    for kind, count in await db.execute(
        select(MessageFeedback.kind, func.count())
        .join(answer, answer.c.id == MessageFeedback.message_id)
        .where(MessageFeedback.org_id == org_id, MessageFeedback.kind != "helpful")
        .group_by(MessageFeedback.kind)
    ):
        wrong[kind] = count
    return wrong


async def _adoption(
    db: AsyncSession, org_id: uuid.UUID, first: datetime, seen: list[uuid.UUID], asked: Any
) -> dict[date, dict[str, int]]:
    """Each week's people asking, and fixes, changes and diagrams started."""
    found: dict[date, dict[str, int]] = {}

    def week_of(day: date) -> dict[str, int]:
        return found.setdefault(_monday(day), dict(_NO_USE))

    monday = cast(func.date_trunc("week", func.timezone("UTC", asked.c.created_at)), Date)
    for when, people in await db.execute(
        select(monday, func.count(distinct(asked.c.user_id))).group_by(monday)
    ):
        week_of(when)["askers"] = people
    fix_day = _utc_day(FixRun.created_at)
    for when, kind, count in await db.execute(
        select(fix_day, FixRun.kind, func.count())
        .where(FixRun.org_id == org_id, FixRun.created_at >= first, FixRun.repo_id.in_(seen))
        .group_by(fix_day, FixRun.kind)
    ):
        week_of(when)["changes" if kind is FixKind.CHANGE else "fixes"] += count
    diagram_day = _utc_day(Diagram.created_at)
    for when, count in await db.execute(
        select(diagram_day, func.count())
        .where(Diagram.org_id == org_id, Diagram.created_at >= first, Diagram.repo_id.in_(seen))
        .group_by(diagram_day)
    ):
        week_of(when)["diagrams"] += count
    return found


async def _modules(
    db: AsyncSession, org_id: uuid.UUID, first: datetime, seen: list[uuid.UUID]
) -> Counter[tuple[uuid.UUID, str]]:
    """How many answers cite a file in each module (an answer counts once per module)."""
    part = (
        func.jsonb_array_elements(ConversationMessage.parts)
        .table_valued(column("value", JSONB))
        .alias("part")
    )
    path = cast(part.c.value["data"]["path"].astext, String)
    cited: dict[tuple[uuid.UUID, str], set[uuid.UUID]] = {}
    for message_id, repo_id, file in await db.execute(
        select(ConversationMessage.id, Conversation.repo_id, path)
        .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
        .join(part, true())
        .where(
            ConversationMessage.org_id == org_id,
            ConversationMessage.role == "assistant",
            ConversationMessage.created_at >= first,
            Conversation.repo_id.in_(seen),
            part.c.value["type"].astext == "data-citation",
        )
        .distinct()
    ):
        if file:
            cited.setdefault((repo_id, module_of(file)), set()).add(message_id)
    return Counter({key: len(messages) for key, messages in cited.items()})
