"""Org and account lifecycle (10.7.3, F19): deleting an org (restorable for a grace period,
then purged with everything in it) and deleting an account.

- **An org** an owner deletes is hidden at once (``deleted_at``). For :data:`ORG_GRACE` its
  owners may restore it; after that a daily worker purges it: its repositories' stored objects,
  then its row, which every org-scoped table follows (``ON DELETE CASCADE``).
- **An account** is refused while its user is the only owner of a live org (someone else must
  own it, or it must be deleted, first). Otherwise its sessions end, its GitHub tokens go, it
  leaves every org, and the user is anonymized: no name, login, email or avatar, and its GitHub
  id is replaced, so the same GitHub account may sign up afresh, as no one this was. What it
  wrote in orgs (questions, diagrams, runs) stays theirs, by an anonymous author.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sensei_core.audit import AuditAction, audit
from sensei_core.db.tenancy import set_org_context, set_user_context
from sensei_core.errors import ConflictError, NotFoundError
from sensei_core.models import Membership, OAuthToken, Org, Repo, Role, User, UserSession
from sensei_core.orgs.service import OrgAccess

ORG_GRACE = timedelta(days=7)
DELETED_LOGIN = "deleted-user"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DeletedOrg:
    org: Org
    purge_at: datetime  # when it can no longer be restored


async def deleted_orgs(
    db: AsyncSession, user: User, *, now: datetime | None = None
) -> list[DeletedOrg]:
    """The deleted orgs the user owns that may still be restored, soonest purged first."""
    await set_user_context(db, user.id)
    moment = _now(now)
    rows = await db.scalars(
        select(Org)
        .join(Membership, Membership.org_id == Org.id)
        .where(
            Membership.user_id == user.id,
            Membership.role == Role.OWNER,
            Org.deleted_at.is_not(None),
            Org.deleted_at > moment - ORG_GRACE,
        )
        .order_by(Org.deleted_at)
    )
    return [DeletedOrg(org, org.deleted_at + ORG_GRACE) for org in rows if org.deleted_at]


async def restore_org(
    db: AsyncSession, user: User, slug: str, *, now: datetime | None = None
) -> OrgAccess:
    """Bring back an org the user owns, deleted within the grace period (404 otherwise, as
    for an org they can't see)."""
    found = next(
        (d.org for d in await deleted_orgs(db, user, now=now) if d.org.slug == slug.lower()),
        None,
    )
    if found is None:
        raise NotFoundError("Organization not found.", code="org_not_found")
    await set_org_context(db, found.id)
    found.deleted_at = None
    await db.flush()
    await audit(
        db,
        AuditAction.ORG_RESTORE,
        actor_user_id=user.id,
        org_id=found.id,
        target_type="org",
        target_id=found.id,
        meta={"slug": found.slug},
    )
    return OrgAccess(org=found, role=Role.OWNER, user=user)


async def orgs_to_purge(system_db: AsyncSession, *, now: datetime | None = None) -> list[Org]:
    """Orgs deleted longer ago than the grace period (``system_db``: reads every org)."""
    moment = _now(now)
    rows = await system_db.scalars(
        select(Org).where(Org.deleted_at.is_not(None), Org.deleted_at <= moment - ORG_GRACE)
    )
    return list(rows)


async def org_repo_ids(system_db: AsyncSession, org_id: uuid.UUID) -> list[uuid.UUID]:
    """Every repository the org has had (disconnected ones too): what storage holds of it."""
    return list(await system_db.scalars(select(Repo.id).where(Repo.org_id == org_id)))


async def drop_org(system_db: AsyncSession, org_id: uuid.UUID) -> bool:
    """The org's row gone, and with it every org-scoped row (only one still deleted: one
    restored meanwhile stays)."""
    result = await system_db.execute(
        delete(Org).where(Org.id == org_id, Org.deleted_at.is_not(None))
    )
    return bool(getattr(result, "rowcount", 0))


async def sole_owned(db: AsyncSession, user: User) -> list[Org]:
    """The live orgs the user is the only owner of."""
    await set_user_context(db, user.id)
    mine = await db.scalars(
        select(Org)
        .join(Membership, Membership.org_id == Org.id)
        .where(
            Membership.user_id == user.id,
            Membership.role == Role.OWNER,
            Org.deleted_at.is_(None),
        )
        .order_by(Org.name)
    )
    other = aliased(Membership)
    found: list[Org] = []
    for org in list(mine):
        await set_org_context(db, org.id)
        owners = await db.scalar(
            select(func.count())
            .select_from(other)
            .where(other.org_id == org.id, other.role == Role.OWNER)
        )
        if (owners or 0) <= 1:
            found.append(org)
    return found


async def delete_account(db: AsyncSession, user: User, *, now: datetime | None = None) -> None:
    """Delete the user's account (see the module); 409 ``sole_owner`` naming the orgs that
    need another owner first."""
    sole = await sole_owned(db, user)
    if sole:
        raise ConflictError(
            "You're the only owner of an organization: make someone else an owner, or delete "
            "it, first.",
            code="sole_owner",
            details={"orgs": [org.slug for org in sole]},
        )
    moment = _now(now)
    await set_user_context(db, user.id)
    await audit(
        db,
        AuditAction.ACCOUNT_DELETE,
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
    )
    memberships = list(await db.scalars(select(Membership).where(Membership.user_id == user.id)))
    for membership in memberships:  # each in its org (row-level security writes need it)
        await set_org_context(db, membership.org_id)
        await db.execute(
            delete(Membership).where(
                Membership.org_id == membership.org_id, Membership.user_id == user.id
            )
        )
        await audit(
            db,
            AuditAction.MEMBER_LEAVE,
            actor_user_id=user.id,
            org_id=membership.org_id,
            target_type="user",
            target_id=user.id,
            meta={"role": membership.role.value, "account_deleted": True},
        )
    await db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=moment)
    )
    await db.execute(delete(OAuthToken).where(OAuthToken.user_id == user.id))
    user.deleted_at = moment
    user.login = DELETED_LOGIN
    user.name = None
    user.email = None
    user.avatar_url = None
    user.last_org_slug = None
    user.is_staff = False
    # Not GitHub's id any more (negative: never one GitHub gives), so the account can sign up
    # again as someone new.
    user.github_id = -secrets.randbelow(2**62) - 1
    await db.flush()
