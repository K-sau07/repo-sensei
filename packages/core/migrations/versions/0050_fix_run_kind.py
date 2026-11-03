# pyright: basic
"""fix run kind

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-03 00:10:02.105009+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


KINDS = ("fix", "change")
SOURCES = ("paste", "github_issue", "sentry")  # before 'request'


def upgrade() -> None:
    sa.Enum(*KINDS, name="fix_kind").create(op.get_bind(), checkfirst=True)
    op.add_column(
        "fix_runs",
        sa.Column(
            "kind", sa.Enum(*KINDS, name="fix_kind", create_type=False), server_default="fix",
            nullable=False,
        ),
    )  # fmt: skip
    op.execute("ALTER TYPE fix_source ADD VALUE IF NOT EXISTS 'request'")
    op.drop_index(op.f("ix_fix_runs_repo_created"), table_name="fix_runs")
    op.create_index(
        "ix_fix_runs_repo_created", "fix_runs", ["repo_id", "kind", "created_at"], unique=False
    )


def downgrade() -> None:
    # Row-level security off, so a role it would filter fails here instead of deleting nothing.
    op.execute("SET LOCAL row_security = off")
    op.execute("DELETE FROM fix_runs WHERE kind = 'change' OR source = 'request'")
    op.drop_index("ix_fix_runs_repo_created", table_name="fix_runs")
    op.create_index(
        op.f("ix_fix_runs_repo_created"), "fix_runs", ["repo_id", "created_at"], unique=False
    )
    op.drop_column("fix_runs", "kind")
    sa.Enum(name="fix_kind").drop(op.get_bind(), checkfirst=True)
    # An enum value can't be dropped: the type is made again without it. The open-run index
    # names no source value, so it survives the column's new type.
    values = ", ".join(f"'{value}'" for value in SOURCES)
    op.execute("ALTER TYPE fix_source RENAME TO fix_source_old")
    op.execute(f"CREATE TYPE fix_source AS ENUM ({values})")
    op.execute(
        "ALTER TABLE fix_runs ALTER COLUMN source TYPE fix_source USING source::text::fix_source"
    )
    op.execute("DROP TYPE fix_source_old")
