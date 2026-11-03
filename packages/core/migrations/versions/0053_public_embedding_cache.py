# pyright: basic
"""public embedding cache

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-03 18:20:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sensei_core.db.tenancy import append_only_sql
from sensei_core.db.types import HalfVector

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Public repositories' vectors, shared by every org (10.1): no org, no row-level security.
    op.create_table(
        "public_embedding_cache",
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("embedding", HalfVector(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(content_hash) = 32", name=op.f("ck_public_embedding_cache_hash_size")
        ),
        sa.CheckConstraint(
            "vector_dims(embedding) = dims", name=op.f("ck_public_embedding_cache_embedding_dims")
        ),
        sa.PrimaryKeyConstraint(
            "content_hash", "model", "dims", name=op.f("pk_public_embedding_cache")
        ),
    )  # fmt: skip
    for statement in append_only_sql("public_embedding_cache"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("public_embedding_cache")
