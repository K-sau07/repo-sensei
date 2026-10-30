# pyright: basic
"""Ask's conversations (4.7): ``conversations`` of a user about a repository, their
``messages`` (each the parts the client renders) and each answer's ``citations``; row-level
security on org_id for all three.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-30 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("conversations", "messages", "citations")


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("share_token_hash", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_conversations_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_conversations_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_conversations_snapshot_id_snapshots"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("id", "org_id", name=op.f("uq_conversations_id_org_id")),
        sa.UniqueConstraint("share_token_hash", name=op.f("uq_conversations_share_token_hash")),
    )
    op.create_index(op.f("ix_conversations_org_id"), "conversations", ["org_id"], unique=False)
    op.create_index(
        "ix_conversations_repo_user_updated",
        "conversations",
        ["repo_id", "user_id", "updated_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversations_snapshot_id"), "conversations", ["snapshot_id"], unique=False
    )
    op.create_table(
        "messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "parts", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("status", sa.String(length=16), server_default="done", nullable=False),
        sa.Column("confidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("abstained", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_messages_role")),
        sa.CheckConstraint(
            "status IN ('streaming', 'done', 'failed')", name=op.f("ck_messages_status")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "org_id"],
            ["conversations.id", "conversations.org_id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_messages_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_messages_snapshot_id_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("id", "org_id", name=op.f("uq_messages_id_org_id")),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_messages_org_id"), "messages", ["org_id"], unique=False)
    op.create_index(op.f("ix_messages_snapshot_id"), "messages", ["snapshot_id"], unique=False)
    op.create_table(
        "citations",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("sha", sa.String(length=40), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("symbol_key", sa.Text(), nullable=True),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("verified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "start_line >= 1 AND end_line >= start_line", name=op.f("ck_citations_lines")
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "org_id"],
            ["messages.id", "messages.org_id"],
            name=op.f("fk_citations_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_citations_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_citations_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citations")),
        sa.UniqueConstraint("message_id", "idx", name=op.f("uq_citations_message_id_idx")),
    )
    op.create_index(op.f("ix_citations_org_id"), "citations", ["org_id"], unique=False)
    op.create_index("ix_citations_repo_id", "citations", ["repo_id"], unique=False)
    for table in TABLES:
        for statement in enable_org_rls_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in reversed(TABLES):
        for statement in disable_org_rls_sql(table):
            op.execute(statement)
    op.drop_index("ix_citations_repo_id", table_name="citations")
    op.drop_index(op.f("ix_citations_org_id"), table_name="citations")
    op.drop_table("citations")
    op.drop_index(op.f("ix_messages_snapshot_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_org_id"), table_name="messages")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_conversations_snapshot_id"), table_name="conversations")
    op.drop_index("ix_conversations_repo_user_updated", table_name="conversations")
    op.drop_index(op.f("ix_conversations_org_id"), table_name="conversations")
    op.drop_table("conversations")
