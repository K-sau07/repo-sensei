"""Organizations, memberships and invitations.

Permission rules (enforced here, and backed by row-level security in the database):

* viewer < member < admin < owner.
* Admins manage members, viewers and other admins; only owners grant, change or remove owners.
* An org always keeps at least one owner (no demoting, removing or leaving as the last owner).
* Non-members cannot tell whether an org exists: lookups fail with ``org_not_found``.

Every change is written to the audit log in the same transaction; no-op requests write nothing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.audit import AuditAction, audit
from sensei_core.auth.sessions import hash_token, new_token
from sensei_core.db.tenancy import set_invite_context, set_org_context, set_user_context
from sensei_core.errors import ConflictError, ForbiddenError, InvalidError, NotFoundError
from sensei_core.mail import InvitationEmail, Mailer
from sensei_core.models import Invitation, Membership, Org, Role, User

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,37}[a-z0-9]$")
RESERVED_SLUGS = frozenset(
    {
        "admin", "api", "app", "auth", "blog", "changelog", "dashboard", "device", "docs",
        "explore", "help", "internal", "invite", "login", "logout", "new", "oauth", "onboarding",
        "pricing", "privacy", "product", "s", "security", "settings", "signup", "static",
        "status", "support", "terms", "www",
    }
)  # fmt: skip
INVITE_TTL = timedelta(days=7)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class OrgAccess:
    """The signed-in user's view of one org; the transaction is already scoped to it."""

    org: Org
    role: Role
    user: User


@dataclass(frozen=True, slots=True)
class InvitationPreview:
    org_name: str
    org_slug: str
    inviter_login: str | None
    role: Role
    status: str  # pending | accepted | revoked | expired


def validate_slug(slug: str) -> str:
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug) or "--" in slug:
        raise InvalidError(
            "Use 3-39 lowercase letters, digits or single hyphens; start and end with a letter "
            "or digit.",
            code="invalid_slug",
        )
    if slug in RESERVED_SLUGS:
        raise InvalidError("That name is reserved.", code="slug_reserved")
    return slug


def require_role(access: OrgAccess, minimum: Role) -> None:
    if not access.role.at_least(minimum):
        raise ForbiddenError(
            f"This needs the {minimum.value} role.",
            code="insufficient_role",
            details={"required_role": minimum.value, "your_role": access.role.value},
        )


class OrgService:
    def __init__(
        self, mailer: Mailer, web_origin: str, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._mailer = mailer
        self.web_origin = web_origin
        self._clock = clock

    # ------------------------------------------------------------------ orgs

    async def slug_available(self, db: AsyncSession, slug: str) -> bool:
        slug = validate_slug(slug)
        return await db.scalar(select(Org.id).where(Org.slug == slug)) is None

    async def create(self, db: AsyncSession, user: User, *, name: str, slug: str) -> OrgAccess:
        slug = validate_slug(slug)
        name = name.strip()
        if not name:
            raise InvalidError("Give the organization a name.", code="invalid_name")
        if not await self.slug_available(db, slug):
            raise ConflictError("That name is already taken.", code="slug_taken")
        org = Org(slug=slug, name=name[:100])
        db.add(org)
        await db.flush()
        await set_org_context(db, org.id)
        db.add(Membership(org_id=org.id, user_id=user.id, role=Role.OWNER))
        user.last_org_slug = slug
        await db.flush()
        await audit(
            db,
            AuditAction.ORG_CREATE,
            actor_user_id=user.id,
            org_id=org.id,
            target_type="org",
            target_id=org.id,
            meta={"slug": slug, "name": org.name},
        )
        return OrgAccess(org=org, role=Role.OWNER, user=user)

    async def list_for_user(self, db: AsyncSession, user: User) -> list[tuple[Org, Role]]:
        await set_user_context(db, user.id)
        rows = await db.execute(
            select(Org, Membership.role)
            .join(Membership, Membership.org_id == Org.id)
            .where(Membership.user_id == user.id, Org.deleted_at.is_(None))
            .order_by(Org.name)
        )
        return [(org, role) for org, role in rows.tuples()]

    async def access(self, db: AsyncSession, user: User, slug: str) -> OrgAccess:
        """Resolve the user's membership in ``slug`` and scope the transaction to that org."""
        await set_user_context(db, user.id)
        row = (
            await db.execute(
                select(Org, Membership.role)
                .join(Membership, Membership.org_id == Org.id)
                .where(
                    Org.slug == slug.lower(),
                    Org.deleted_at.is_(None),
                    Membership.user_id == user.id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("Organization not found.", code="org_not_found")
        org, role = row[0], row[1]
        await set_org_context(db, org.id)
        return OrgAccess(org=org, role=role, user=user)

    async def update(
        self, db: AsyncSession, access: OrgAccess, *, name: str | None, slug: str | None
    ) -> Org:
        require_role(access, Role.ADMIN)
        org = access.org
        changes: dict[str, list[str]] = {}
        if name is not None:
            if not name.strip():
                raise InvalidError("Give the organization a name.", code="invalid_name")
            new_name = name.strip()[:100]
            if new_name != org.name:
                changes["name"] = [org.name, new_name]
                org.name = new_name
        if slug is not None and slug.lower() != org.slug:
            slug = validate_slug(slug)
            if not await self.slug_available(db, slug):
                raise ConflictError("That name is already taken.", code="slug_taken")
            changes["slug"] = [org.slug, slug]
            org.slug = slug
        await db.flush()
        if changes:
            await self._audit(db, access, AuditAction.ORG_UPDATE, "org", org.id, meta=changes)
        return org

    async def delete(self, db: AsyncSession, access: OrgAccess) -> None:
        """Soft delete; a later purge job removes the data after the grace period."""
        require_role(access, Role.OWNER)
        access.org.deleted_at = self._clock()
        await db.flush()
        await self._audit(db, access, AuditAction.ORG_DELETE, "org", access.org.id)

    # ------------------------------------------------------------------ members

    async def list_members(
        self, db: AsyncSession, access: OrgAccess
    ) -> list[tuple[User, Membership]]:
        rows = await db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == access.org.id)
            .order_by(Membership.created_at)
        )
        return [(user, membership) for user, membership in rows.tuples()]

    async def _membership(
        self, db: AsyncSession, access: OrgAccess, user_id: uuid.UUID
    ) -> Membership:
        membership = await db.get(Membership, (access.org.id, user_id))
        if membership is None:
            raise NotFoundError("Member not found.", code="member_not_found")
        return membership

    async def _owner_count(self, db: AsyncSession, org_id: uuid.UUID) -> int:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.org_id == org_id, Membership.role == Role.OWNER)
            )
            or 0
        )

    async def _ensure_not_last_owner(self, db: AsyncSession, membership: Membership) -> None:
        if membership.role is Role.OWNER and await self._owner_count(db, membership.org_id) <= 1:
            raise ConflictError("An organization needs at least one owner.", code="last_owner")

    async def change_role(
        self, db: AsyncSession, access: OrgAccess, user_id: uuid.UUID, role: Role
    ) -> Membership:
        require_role(access, Role.ADMIN)
        membership = await self._membership(db, access, user_id)
        if Role.OWNER in (role, membership.role):
            require_role(access, Role.OWNER)
        if membership.role is Role.OWNER and role is not Role.OWNER:
            await self._ensure_not_last_owner(db, membership)
        if membership.role is role:
            return membership
        previous = membership.role
        membership.role = role
        await db.flush()
        await self._audit(
            db,
            access,
            AuditAction.MEMBER_ROLE_CHANGE,
            "user",
            user_id,
            meta={"from": previous.value, "to": role.value},
        )
        return membership

    async def remove_member(self, db: AsyncSession, access: OrgAccess, user_id: uuid.UUID) -> None:
        require_role(access, Role.ADMIN)
        membership = await self._membership(db, access, user_id)
        if membership.role is Role.OWNER:
            require_role(access, Role.OWNER)
            await self._ensure_not_last_owner(db, membership)
        await db.delete(membership)
        await db.flush()
        await self._audit(
            db,
            access,
            AuditAction.MEMBER_REMOVE,
            "user",
            user_id,
            meta={"role": membership.role.value},
        )

    async def leave(self, db: AsyncSession, access: OrgAccess) -> None:
        membership = await self._membership(db, access, access.user.id)
        await self._ensure_not_last_owner(db, membership)
        await db.delete(membership)
        await db.flush()
        await self._audit(
            db,
            access,
            AuditAction.MEMBER_LEAVE,
            "user",
            access.user.id,
            meta={"role": membership.role.value},
        )

    async def _audit(
        self,
        db: AsyncSession,
        access: OrgAccess,
        action: AuditAction,
        target_type: str,
        target_id: uuid.UUID,
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        await audit(
            db,
            action,
            actor_user_id=access.user.id,
            org_id=access.org.id,
            target_type=target_type,
            target_id=target_id,
            meta=meta,
        )

    # ------------------------------------------------------------------ invitations

    async def invite(
        self,
        db: AsyncSession,
        access: OrgAccess,
        *,
        role: Role,
        email: str | None = None,
        github_login: str | None = None,
    ) -> tuple[Invitation, str]:
        """Create an invitation and send it. Returns the invitation and its one-time token."""
        require_role(access, Role.ADMIN)
        if role is Role.OWNER:
            require_role(access, Role.OWNER)
        email = (email or "").strip() or None
        github_login = (github_login or "").strip().lstrip("@") or None
        if not (email or github_login):
            raise InvalidError("Invite by email or GitHub username.", code="invite_target_required")
        token = new_token()
        invitation = Invitation(
            org_id=access.org.id,
            email=email,
            github_login=github_login,
            role=role,
            token_hash=hash_token(token),
            invited_by=access.user.id,
            expires_at=self._clock() + INVITE_TTL,
        )
        db.add(invitation)
        await db.flush()
        await self._audit(
            db,
            access,
            AuditAction.INVITATION_CREATE,
            "invitation",
            invitation.id,
            meta={"role": role.value, "email": email, "github_login": github_login},
        )
        if email:
            await self._mailer.send_invitation(
                InvitationEmail(
                    to=email,
                    org_name=access.org.name,
                    inviter=access.user.login,
                    role=role.value,
                    link=f"{self.web_origin}/invite/{token}",
                )
            )
        return invitation, token

    async def list_invitations(self, db: AsyncSession, access: OrgAccess) -> list[Invitation]:
        require_role(access, Role.ADMIN)
        result = await db.scalars(
            select(Invitation)
            .where(
                Invitation.org_id == access.org.id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > self._clock(),
            )
            .order_by(Invitation.created_at.desc())
        )
        return list(result.all())

    async def revoke_invitation(
        self, db: AsyncSession, access: OrgAccess, invitation_id: uuid.UUID
    ) -> None:
        require_role(access, Role.ADMIN)
        invitation = await db.get(Invitation, invitation_id)
        if invitation is None or invitation.org_id != access.org.id:
            raise NotFoundError("Invitation not found.", code="invitation_not_found")
        if invitation.revoked_at is None and invitation.accepted_at is None:
            invitation.revoked_at = self._clock()
            await db.flush()
            await self._audit(
                db, access, AuditAction.INVITATION_REVOKE, "invitation", invitation.id
            )

    def _status(self, invitation: Invitation) -> str:
        if invitation.revoked_at is not None:
            return "revoked"
        if invitation.accepted_at is not None:
            return "accepted"
        if invitation.expires_at <= self._clock():
            return "expired"
        return "pending"

    async def _invitation_by_token(self, db: AsyncSession, token: str) -> Invitation:
        token_hash = hash_token(token)
        await set_invite_context(db, token_hash)
        invitation = await db.scalar(select(Invitation).where(Invitation.token_hash == token_hash))
        if invitation is None:
            raise NotFoundError("This invitation link is not valid.", code="invitation_not_found")
        return invitation

    async def preview_invitation(self, db: AsyncSession, token: str) -> InvitationPreview:
        invitation = await self._invitation_by_token(db, token)
        org = await db.get(Org, invitation.org_id)
        if org is None or org.deleted_at is not None:
            raise NotFoundError("This invitation link is not valid.", code="invitation_not_found")
        inviter = await db.get(User, invitation.invited_by) if invitation.invited_by else None
        return InvitationPreview(
            org_name=org.name,
            org_slug=org.slug,
            inviter_login=inviter.login if inviter else None,
            role=invitation.role,
            status=self._status(invitation),
        )

    async def accept_invitation(self, db: AsyncSession, user: User, token: str) -> OrgAccess:
        invitation = await self._invitation_by_token(db, token)
        status = self._status(invitation)
        if status != "pending":
            raise ConflictError(f"This invitation was {status}.", code=f"invitation_{status}")
        self._check_invitee(invitation, user)

        org = await db.get(Org, invitation.org_id)
        if org is None or org.deleted_at is not None:
            raise NotFoundError("This invitation link is not valid.", code="invitation_not_found")
        await set_org_context(db, org.id)
        membership = await db.get(Membership, (org.id, user.id))
        if membership is None:
            membership = Membership(org_id=org.id, user_id=user.id, role=invitation.role)
            db.add(membership)
        invitation.accepted_at = self._clock()
        invitation.accepted_by = user.id
        user.last_org_slug = org.slug
        await db.flush()
        await audit(
            db,
            AuditAction.INVITATION_ACCEPT,
            actor_user_id=user.id,
            org_id=org.id,
            target_type="invitation",
            target_id=invitation.id,
            meta={"role": membership.role.value},
        )
        return OrgAccess(org=org, role=membership.role, user=user)

    @staticmethod
    def _check_invitee(invitation: Invitation, user: User) -> None:
        matches_email = bool(
            invitation.email and user.email and invitation.email.lower() == user.email.lower()
        )
        matches_login = bool(
            invitation.github_login and invitation.github_login.lower() == user.login.lower()
        )
        if not (matches_email or matches_login):
            raise ForbiddenError(
                "This invitation was sent to a different account.",
                code="invitation_wrong_account",
            )
