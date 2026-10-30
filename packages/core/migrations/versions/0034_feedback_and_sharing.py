# pyright: basic
"""Answers' feedback and shared conversations (4.11): ``message_feedback``, what a user said
of an answer (helpful, or what was wrong with it), with row-level security on org_id; and a
policy that lets a share link read its one conversation (by the token's hash).

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-30 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql, shared_rls_sql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_feedback",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('helpful', 'wrong_file', 'outdated', 'missing_context', 'invented_symbol',"
            " 'other')",
            name=op.f("ck_message_feedback_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "org_id"],
            ["messages.id", "messages.org_id"],
            name=op.f("fk_message_feedback_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_message_feedback_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_message_feedback_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_feedback")),
        sa.UniqueConstraint(
            "message_id", "user_id", name=op.f("uq_message_feedback_message_id_user_id")
        ),
    )
    op.create_index(
        op.f("ix_message_feedback_org_id"), "message_feedback", ["org_id"], unique=False
    )
    for statement in enable_org_rls_sql("message_feedback"):
        op.execute(statement)
    for statement in shared_rls_sql("conversations"):
        op.execute(statement)


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS share_by_token ON "conversations"')
    for statement in disable_org_rls_sql("message_feedback"):
        op.execute(statement)
    op.drop_index(op.f("ix_message_feedback_org_id"), table_name="message_feedback")
    op.drop_table("message_feedback")
