# pyright: basic
"""The answer cache (4.9): answers kept for the same question at the same commit and mode,
with the question's embedding for near matches and the hashes of the files they cite; row-
level security on org_id.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-30 16:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql
from sensei_core.db.types import HalfVector

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "answer_cache",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("question_key", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("embedding", HalfVector(1024), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column(
            "file_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id", "org_id"],
            ["messages.id", "messages.org_id"],
            name=op.f("fk_answer_cache_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_answer_cache_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_answer_cache_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "org_id"],
            ["snapshots.id", "snapshots.org_id"],
            name=op.f("fk_answer_cache_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_answer_cache")),
    )
    op.create_index(
        "ix_answer_cache_lookup",
        "answer_cache",
        ["snapshot_id", "mode", "question_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_answer_cache_message_id"), "answer_cache", ["message_id"], unique=False
    )
    op.create_index(op.f("ix_answer_cache_org_id"), "answer_cache", ["org_id"], unique=False)
    op.create_index("ix_answer_cache_repo_id", "answer_cache", ["repo_id"], unique=False)
    for statement in enable_org_rls_sql("answer_cache"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_org_rls_sql("answer_cache"):
        op.execute(statement)
    op.drop_index("ix_answer_cache_repo_id", table_name="answer_cache")
    op.drop_index(op.f("ix_answer_cache_org_id"), table_name="answer_cache")
    op.drop_index(op.f("ix_answer_cache_message_id"), table_name="answer_cache")
    op.drop_index("ix_answer_cache_lookup", table_name="answer_cache")
    op.drop_table("answer_cache")
