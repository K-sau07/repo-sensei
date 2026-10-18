"""Users, their stored OAuth tokens, and browser sessions.

None of these tables are org-scoped: a user exists independently of the orgs they belong to, and
access goes through services that always filter by user.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, INET
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    login: Mapped[str] = mapped_column(String(39))
    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(CITEXT)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    is_staff: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    last_org_slug: Mapped[str | None] = mapped_column(CITEXT)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthToken(IdMixin, TimestampMixin, Base):
    """Provider tokens, encrypted at rest with the user id bound as associated data."""

    __tablename__ = "oauth_tokens"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))


class UserSession(IdMixin, Base):
    """A signed-in browser. Only SHA-256 hashes of the session and CSRF tokens are stored."""

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
