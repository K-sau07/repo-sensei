# pyright: basic
"""web vitals measured in the app's pages

Revision ID: 0059
Revises: 0058
Create Date: 2026-11-10 12:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One row per measure of a page view (10.11), its route a pattern: no names.
    op.create_table(
        "web_vitals",
        sa.Column("name", sa.String(length=8), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("route", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_web_vitals_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_vitals")),
    )
    op.create_index(op.f("ix_web_vitals_org_id"), "web_vitals", ["org_id"], unique=False)
    op.create_index("ix_web_vitals_org_created", "web_vitals", ["org_id", "created_at"])
    for statement in enable_org_rls_sql("web_vitals"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_org_rls_sql("web_vitals"):
        op.execute(statement)
    op.drop_index("ix_web_vitals_org_created", table_name="web_vitals")
    op.drop_index(op.f("ix_web_vitals_org_id"), table_name="web_vitals")
    op.drop_table("web_vitals")
