# pyright: basic
"""OAuth grants remember whether the authorization request named its redirect URI: then the
token request must name it too (OAuth 2.1 §4.1.3). Rows from before are taken to have.

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-01 19:40:31.278367+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_grants",
        sa.Column("redirect_given", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("oauth_grants", "redirect_given")
