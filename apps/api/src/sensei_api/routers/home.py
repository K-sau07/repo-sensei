"""An organization's home (4.13): the repositories the signed-in user can read, the agents
running in them now, and the user's latest questions across them.

Only what is stored is shown: there is no inbox of things waiting on the user yet (no reviews or
mentions are recorded), so the home has none rather than an empty list pretending otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from sensei_api.deps import DbSession
from sensei_api.orgs import OrgAccessDep
from sensei_api.repos import RepoAccessDep, RepoServiceDep, checked
from sensei_api.routers.ask import ConversationOut, conversation_out
from sensei_api.routers.repos import RepoOut
from sensei_api.routers.runs import RunOut, run_out
from sensei_api.spend import HOLD_SECONDS
from sensei_core.indexing.jobs import repo_summaries
from sensei_core.models import AgentRun, Conversation, User

router = APIRouter(prefix="/orgs/{org}", tags=["home"])

MAX_LIVE = 10
MAX_RECENT = 8
# A run can't outlast its budget's hold: one still "running" past it was cut off (a process
# that died without stopping its runs), not live.
LIVE_WINDOW = timedelta(seconds=HOLD_SECONDS)


class HomeOut(BaseModel):
    repos: list[RepoOut]
    live: list[RunOut]  # running now, the latest first
    recent: list[ConversationOut]  # the user's, the latest first


@router.get("/home", response_model=HomeOut)
async def home(
    access: OrgAccessDep, db: DbSession, repos: RepoServiceDep, readers: RepoAccessDep
) -> HomeOut:
    """What the signed-in user sees first: their repositories, what is running in them, and
    what they asked last."""
    visible = await checked(readers, access.user, await repos.list(db, access))
    ids = [repo.id for repo in visible]
    summaries = await repo_summaries(db, visible)
    live = (
        list(
            await db.execute(
                select(AgentRun, User.login)
                .outerjoin(User, User.id == AgentRun.user_id)
                .where(
                    AgentRun.repo_id.in_(ids),
                    AgentRun.status == "running",
                    AgentRun.finished_at.is_(None),
                    AgentRun.created_at > datetime.now(UTC) - LIVE_WINDOW,
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(MAX_LIVE)
            )
        )
        if ids
        else []
    )
    recent = (
        list(
            await db.scalars(
                select(Conversation)
                .where(Conversation.repo_id.in_(ids), Conversation.user_id == access.user.id)
                .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                .limit(MAX_RECENT)
            )
        )
        if ids
        else []
    )
    return HomeOut(
        repos=[RepoOut.build(repo, summary=summaries.get(repo.id)) for repo in visible],
        live=[run_out(run, login) for run, login in live],
        recent=[conversation_out(found) for found in recent],
    )
