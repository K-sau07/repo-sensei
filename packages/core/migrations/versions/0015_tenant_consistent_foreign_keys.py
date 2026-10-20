# pyright: basic
"""Child rows reference their repository (and snapshot) together with org_id, so a row can't
point at another org's repository even if a bug hands it the wrong id (foreign keys ignore
row-level security).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-20 17:50:29.933546+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, constraint, columns, referred table, referred columns)
COMPOSITE = (
    ("snapshots", "fk_snapshots_repo_id_repos", ["repo_id", "org_id"], "repos", ["id", "org_id"]),
    ("index_jobs", "fk_index_jobs_repo_id_repos", ["repo_id", "org_id"], "repos", ["id", "org_id"]),
    ("files", "fk_files_repo_id_repos", ["repo_id", "org_id"], "repos", ["id", "org_id"]),
    (
        "files",
        "fk_files_snapshot_id_snapshots",
        ["snapshot_id", "org_id"],
        "snapshots",
        ["id", "org_id"],
    ),
)


def upgrade() -> None:
    op.create_unique_constraint("uq_repos_id_org_id", "repos", ["id", "org_id"])
    op.create_unique_constraint("uq_snapshots_id_org_id", "snapshots", ["id", "org_id"])
    for table, name, columns, referred, referred_columns in COMPOSITE:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, referred, columns, referred_columns, ondelete="CASCADE")


def downgrade() -> None:
    for table, name, columns, referred, referred_columns in reversed(COMPOSITE):
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, referred, columns[:1], referred_columns[:1], ondelete="CASCADE"
        )
    op.drop_constraint("uq_snapshots_id_org_id", "snapshots", type_="unique")
    op.drop_constraint("uq_repos_id_org_id", "repos", type_="unique")
