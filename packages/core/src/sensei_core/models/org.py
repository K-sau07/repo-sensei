"""Organizations, memberships and invitations."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin
from sensei_core.db.tenancy import OrgScopedMixin


class Role(enum.StrEnum):
    """Ordered from least to most privileged."""

    VIEWER = "viewer"
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return list(Role).index(self)

    def at_least(self, other: Role) -> bool:
        return self.rank >= other.rank


def _role_values(roles: type[Role]) -> list[str]:
    return [role.value for role in roles]


role_enum = Enum(Role, name="org_role", values_callable=_role_values)


class Org(IdMixin, TimestampMixin, Base):
    """The tenant. Not row-level-secured itself: access always goes through a membership."""

    __tablename__ = "orgs"

    slug: Mapped[str] = mapped_column(CITEXT, unique=True)
    name: Mapped[str] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[str] = mapped_column(String(32), server_default=text("'free'"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The steps an admin raised to a larger model or more effort (sensei_core.orgs.model_choices).
    model_choices: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class Membership(OrgScopedMixin, Base):
    __tablename__ = "memberships"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[Role] = mapped_column(role_enum)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Invitation(IdMixin, OrgScopedMixin, Base):
    """An invitation link. Only the SHA-256 hash of its token is stored."""

    __tablename__ = "invitations"

    email: Mapped[str | None] = mapped_column(CITEXT)
    github_login: Mapped[str | None] = mapped_column(CITEXT)
    role: Mapped[Role] = mapped_column(role_enum)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
