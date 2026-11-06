# pyright: basic
"""staff read every org's memberships, repositories, index runs and usage (the admin console)

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-06 10:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from sensei_core.db.tenancy import staff_read_sql

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("memberships", "repos", "index_jobs", "usage_events")


def upgrade() -> None:
    # SELECT only, and only while the transaction names a user the users table says is staff.
    for table in TABLES:
        for statement in staff_read_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f'DROP POLICY IF EXISTS staff_read ON "{table}"')
