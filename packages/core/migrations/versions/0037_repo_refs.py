# pyright: basic
"""What each ref reads (5.4): ``repo_refs``, a repository's branch or pull request and the snapshot
its index is (a snapshot is one per commit, so two refs at one commit share it: a branch made
from the default branch, a fast-forward). Row-level security on org_id. Filled from what is
indexed now: the default branch's last index, and each other ref's latest ready snapshot.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-31 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repo_refs",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id", "org_id"],
            ["repos.id", "repos.org_id"],
            name=op.f("fk_repo_refs_repo_id_repos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_repo_refs_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_repo_refs_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repo_refs")),
        sa.UniqueConstraint("repo_id", "ref", name=op.f("uq_repo_refs_repo_id_ref")),
    )
    op.create_index(op.f("ix_repo_refs_org_id"), "repo_refs", ["org_id"], unique=False)
    op.create_index(op.f("ix_repo_refs_snapshot_id"), "repo_refs", ["snapshot_id"], unique=False)
    op.execute(
        "INSERT INTO repo_refs (org_id, repo_id, ref, snapshot_id) "
        "SELECT r.org_id, r.id, r.default_branch, s.id FROM repos r "
        "JOIN snapshots s ON s.repo_id = r.id AND s.commit_sha = r.last_indexed_sha "
        "WHERE r.last_indexed_sha IS NOT NULL"
    )
    op.execute(
        "INSERT INTO repo_refs (org_id, repo_id, ref, snapshot_id) "
        "SELECT DISTINCT ON (s.repo_id, s.ref) s.org_id, s.repo_id, s.ref, s.id "
        "FROM snapshots s JOIN repos r ON r.id = s.repo_id "
        "WHERE s.status = 'ready' AND s.ref <> r.default_branch "
        "ORDER BY s.repo_id, s.ref, s.ready_at DESC NULLS LAST, s.created_at DESC "
        "ON CONFLICT (repo_id, ref) DO NOTHING"
    )
    for statement in enable_org_rls_sql("repo_refs"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_org_rls_sql("repo_refs"):
        op.execute(statement)
    op.drop_index(op.f("ix_repo_refs_snapshot_id"), table_name="repo_refs")
    op.drop_index(op.f("ix_repo_refs_org_id"), table_name="repo_refs")
    op.drop_table("repo_refs")
