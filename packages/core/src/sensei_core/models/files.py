"""The files of a snapshot that indexing keeps, with what discovery found out about each."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKeyConstraint,
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
        # Fuzzy path search (⌘K): trigram matching on the path.
        Index(
            "ix_files_path_trgm",
            "path",
            postgresql_using="gin",
            postgresql_ops={"path": "gin_trgm_ops"},
        ),
        # With org_id: a file can't point at another org's repository or snapshot.
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "org_id"], ["snapshots.id", "snapshots.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    snapshot_id: Mapped[uuid.UUID] = mapped_column()
    path: Mapped[str] = mapped_column(Text)
    blob_sha: Mapped[str] = mapped_column(String(64))  # git's object id for the content
    content_hash: Mapped[bytes] = mapped_column(LargeBinary)  # SHA-256 of the content
    # xxh3-128 of the content, for quick checks against a file on disk (None: discovered before
    # it was recorded; the next index fills it in).
    fast_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    language: Mapped[str | None] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(BigInteger)
    loc: Mapped[int | None] = mapped_column(Integer)  # physical lines; None if not read as text
    role: Mapped[str] = mapped_column(String(16))
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    framework_root: Mapped[str | None] = mapped_column(Text)
