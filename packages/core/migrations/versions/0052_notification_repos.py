# pyright: basic
"""notification repos

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-03 09:40:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The repository a notification is about: shown only to those who can read it.
    for table in ("notifications", "notification_deliveries"):
        op.add_column(table, sa.Column("repo_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_repo_id_repos",
            table,
            "repos",
            ["repo_id", "org_id"],
            ["id", "org_id"],
            ondelete="CASCADE",
        )
    # An email's person (whose access is checked before it's sent), and when to try again.
    op.add_column("notification_deliveries", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_notification_deliveries_user_id_users",
        "notification_deliveries",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_deliveries", "next_attempt_at")
    op.drop_constraint(
        "fk_notification_deliveries_user_id_users", "notification_deliveries", type_="foreignkey"
    )
    op.drop_column("notification_deliveries", "user_id")
    for table in ("notification_deliveries", "notifications"):
        op.drop_constraint(f"fk_{table}_repo_id_repos", table, type_="foreignkey")
        op.drop_column(table, "repo_id")
