# pyright: basic
"""A GitHub repository is unique among an org's live repositories only: connecting one again
after a disconnect makes a new row while the old one waits for its purge.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20 17:35:50.726464+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("uq_repos_org_id_github_repo_id"), "repos", type_="unique")
    op.create_index(
        "uq_repos_org_id_github_repo_id_live",
        "repos",
        ["org_id", "github_repo_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_repos_org_id_github_repo_id_live",
        table_name="repos",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_unique_constraint(
        op.f("uq_repos_org_id_github_repo_id"),
        "repos",
        ["org_id", "github_repo_id"],
    )
