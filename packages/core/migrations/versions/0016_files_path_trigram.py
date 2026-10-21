# pyright: basic
"""Fuzzy path search: the pg_trgm extension and a trigram index on files.path.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-20 20:30:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pg_trgm is a trusted extension: the database owner (who runs migrations) may create it.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_files_path_trgm",
        "files",
        ["path"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"path": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_files_path_trgm", table_name="files", postgresql_using="gin")
    # The extension stays: other database objects may come to rely on it.
