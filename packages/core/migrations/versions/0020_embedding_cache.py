# pyright: basic
"""Embedding cache: one half-precision vector per content hash, model and size, shared by every
org (content-addressed, no text, no row-level security); the app role may only read and add.

Revision ID: 0020
Revises: 0018
Create Date: 2026-10-23 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sensei_core.db.tenancy import append_only_sql
from sensei_core.db.types import HalfVector

revision: str = "0020"
# 0019 is the chunks table (2.6); this is re-pointed at it when both are merged.
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embedding_cache",
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("embedding", HalfVector(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(content_hash) = 32", name=op.f("ck_embedding_cache_hash_size")
        ),
        sa.CheckConstraint(
            "vector_dims(embedding) = dims", name=op.f("ck_embedding_cache_embedding_dims")
        ),
        sa.PrimaryKeyConstraint("content_hash", "model", "dims", name=op.f("pk_embedding_cache")),
    )
    for statement in append_only_sql("embedding_cache"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("embedding_cache")
