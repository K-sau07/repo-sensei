"""The files of a snapshot that indexing keeps, with what discovery found out about each."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin
from sensei_core.db.tenancy import OrgScopedMixin


class RepoFile(IdMixin, OrgScopedMixin, Base):
    """One file at one commit. ``path`` is relative to the repository root (POSIX separators).

    ``role``: ui, api, service, db, test, config, docs or other. ``flags``: binary, generated,
    vendored, too_large, lfs_pointer, documentation. ``framework_root``: the package directory whose
    manifest the file falls under ("." for the root), if any. Written in bulk by discovery."""

    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "path"),
        Index("ix_files_repo_id", "repo_id"),  # deleting a repository cascades here
    )

    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    blob_sha: Mapped[str] = mapped_column(String(64))  # git's object id for the content
    content_hash: Mapped[bytes] = mapped_column(LargeBinary)  # SHA-256 of the content
    language: Mapped[str | None] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(BigInteger)
    loc: Mapped[int | None] = mapped_column(Integer)  # physical lines; None if not read as text
    role: Mapped[str] = mapped_column(String(16))
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    framework_root: Mapped[str | None] = mapped_column(Text)
