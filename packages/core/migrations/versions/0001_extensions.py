"""Enable the postgres extensions the engine relies on.

Revision ID: 0001
Revises:
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("vector", "pg_trgm", "pg_textsearch", "citext")


def upgrade() -> None:
    for name in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{name}"')


def downgrade() -> None:
    # Extensions are shared infrastructure; dropping them could destroy data owned by other
    # objects, so downgrade intentionally leaves them installed.
    pass
