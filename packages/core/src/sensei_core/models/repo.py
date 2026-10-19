"""Repositories connected to an org."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin
from sensei_core.db.tenancy import OrgScopedMixin


class RepoSource(enum.StrEnum):
    GITHUB_APP = "github_app"  # through one of the org's GitHub App installations
    PUBLIC_URL = "public_url"  # a public repository imported by URL


class RepoStatus(enum.StrEnum):
    PENDING = "pending"  # connected, not indexed yet
    INDEXING = "indexing"
    READY = "ready"
    ERROR = "error"
    ACCESS_LOST = "access_lost"  # the installation was removed or no longer covers it
    ARCHIVED = "archived"


def _values(enum_type: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class Repo(IdMixin, OrgScopedMixin, TimestampMixin, Base):
    """A GitHub repository connected to an org. ``slug`` names it in URLs (``/acme/langflow``),
    unique among the org's live repositories."""

    __tablename__ = "repos"
    __table_args__ = (
        UniqueConstraint("org_id", "github_repo_id"),
        Index(
            "uq_repos_org_id_slug_live",
            "org_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    installation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("github_installations.id", ondelete="SET NULL"), index=True
    )
    source: Mapped[RepoSource] = mapped_column(
        Enum(RepoSource, name="repo_source", values_callable=_values)
    )
    github_repo_id: Mapped[int] = mapped_column(BigInteger)
    owner: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    full_name: Mapped[str] = mapped_column(String(201))
    slug: Mapped[str] = mapped_column(String(201))
    default_branch: Mapped[str] = mapped_column(String(255))
    visibility: Mapped[str] = mapped_column(String(16))  # public, private or internal
    size_kb: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    languages: Mapped[dict[str, int]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[RepoStatus] = mapped_column(
        Enum(RepoStatus, name="repo_status", values_callable=_values),
        default=RepoStatus.PENDING,
        server_default=RepoStatus.PENDING.value,
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    last_indexed_sha: Mapped[str | None] = mapped_column(String(64))
    connected_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
