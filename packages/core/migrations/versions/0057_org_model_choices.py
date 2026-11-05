# pyright: basic
"""each org's model and effort per step

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-05 10:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The steps an admin raised (10.7.2); a step left out runs as recommended.
    op.add_column(
        "orgs",
        sa.Column(
            "model_choices",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("orgs", "model_choices")
