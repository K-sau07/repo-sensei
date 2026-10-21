"""GitHub App installations linked to orgs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin
from sensei_core.db.tenancy import OrgScopedMixin


class GitHubInstallation(IdMixin, OrgScopedMixin, TimestampMixin, Base):
    """One GitHub account (user or organization) that installed the app for one of our orgs.

    An installation belongs to exactly one org (`installation_id` is unique)."""

    __tablename__ = "github_installations"

    installation_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    account_login: Mapped[str] = mapped_column(String(100))
    account_type: Mapped[str] = mapped_column(String(32))  # "User" or "Organization"
    account_id: Mapped[int] = mapped_column(BigInteger)
    repository_selection: Mapped[str] = mapped_column(String(16))  # "all" or "selected"
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    events: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default=text("'{}'"))
    linked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
