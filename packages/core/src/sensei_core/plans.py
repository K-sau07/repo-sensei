"""The free plan (10.14): what an org on it may do, and the counts that keep it to that.

Every limit is checked in the API, before anything is spent, so the web app, the MCP server, the
command line and API keys all meet the same one. A count is taken in the request's transaction:
a refused or failed request takes none. An org on any other plan has none of these limits, and
none apply until ``LimitsSettings.free_plan`` is on.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.config import LimitsSettings
from sensei_core.errors import ForbiddenError
from sensei_core.models import IndexJob, Org, Repo, Role, User
from sensei_core.orgs.service import OrgAccess

FREE = "free"

Trial = Literal["asks", "diagrams"]

_WHAT: dict[Trial, tuple[str, str]] = {
    "asks": ("question", "questions"),
    "diagrams": ("diagram drawn by the model", "diagrams drawn by the model"),
}


def limited(org: Org, limits: LimitsSettings) -> bool:
    """Whether ``org`` is held to the free plan's limits."""
    return limits.free_plan and org.plan == FREE


def _refused(what: str, limit: int, trial: str) -> ForbiddenError:
    return ForbiddenError(
        f"The free trial covers {limit} {what}, and it's been used. More takes a subscription.",
        code="trial_limit_reached",
        details={"trial": trial, "limit": limit},
    )


async def take(db: AsyncSession, access: OrgAccess, limits: LimitsSettings, trial: Trial) -> None:
    """One of the person's free questions or model-drawn diagrams, or 403 ``trial_limit_reached``.
    Counted for the person, whichever org on the free plan it's in."""
    if not limited(access.org, limits):
        return
    limit = limits.free_asks if trial == "asks" else limits.free_diagrams
    column = User.trial_asks if trial == "asks" else User.trial_diagrams
    one, many = _WHAT[trial]
    taken = await db.scalar(
        update(User)
        .where(User.id == access.user.id, column < limit)
        .values({column: column + 1})
        .returning(column)
    )
    if taken is None:
        raise _refused(one if limit == 1 else many, limit, trial)


async def check_reindex(
    db: AsyncSession, access: OrgAccess, repo: Repo, limits: LimitsSettings
) -> None:
    """A repository on the free plan is indexed once and again ``free_reindexes`` times by hand,
    or 403 ``trial_limit_reached``."""
    if not limited(access.org, limits):
        return
    runs = await db.scalar(
        select(func.count()).select_from(IndexJob).where(IndexJob.repo_id == repo.id)
    )
    if (runs or 0) >= 1 + limits.free_reindexes:
        what = "re-index" if limits.free_reindexes == 1 else "re-indexes"
        raise _refused(what, limits.free_reindexes, "reindexes")


def check_new_org(user: User, mine: list[tuple[Org, Role]], limits: LimitsSettings) -> None:
    """A person owns ``free_orgs_per_person`` orgs at most (staff aside), or 403
    ``org_limit_reached``: each new org is on the free plan. ``mine``: the person's orgs, as
    :meth:`OrgService.list_for_user` reads them."""
    if not limits.free_plan or user.is_staff:
        return
    owned = sum(1 for _, role in mine if role is Role.OWNER)
    if owned >= limits.free_orgs_per_person:
        raise ForbiddenError(
            f"You can own up to {limits.free_orgs_per_person} organizations on the free plan.",
            code="org_limit_reached",
            details={"limit": limits.free_orgs_per_person},
        )
