# pyright: basic
"""Snapshots and index jobs (row-level security on org_id).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20 13:42:32.716029+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from sensei_core.db.tenancy import disable_org_rls_sql, enable_org_rls_sql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SNAPSHOT_STATUS = postgresql.ENUM(
    "pending", "ready", "failed", name="snapshot_status", create_type=False
)
JOB_KIND = postgresql.ENUM("full", "incremental", name="index_job_kind", create_type=False)
JOB_STATUS = postgresql.ENUM(
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    name="index_job_status",
    create_type=False,
)
STAGE = postgresql.ENUM(
    "clone",
    "discover",
    "merkle",
    "parse",
    "symbols",
    "chunks",
    "embeddings",
    "summaries",
    "graph",
    "links",
    "finalize",
    name="index_stage",
    create_type=False,
)
ENUMS = (SNAPSHOT_STATUS, JOB_KIND, JOB_STATUS, STAGE)


def upgrade() -> None:
    for enum_type in ENUMS:
        enum_type.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "snapshots",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            SNAPSHOT_STATUS,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("file_count", sa.Integer(), nullable=True),
        sa.Column("loc", sa.BigInteger(), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("merkle_root", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_snapshots_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_snapshots_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_snapshots")),
        sa.UniqueConstraint("repo_id", "commit_sha", name=op.f("uq_snapshots_repo_id_commit_sha")),
    )
    op.create_index(op.f("ix_snapshots_org_id"), "snapshots", ["org_id"], unique=False)
    op.create_table(
        "index_jobs",
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("kind", JOB_KIND, nullable=False),
        sa.Column(
            "status",
            JOB_STATUS,
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "stage",
            STAGE,
            nullable=True,
        ),
        sa.Column(
            "stages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("progress", sa.Float(), server_default="0", nullable=False),
        sa.Column("eta_s", sa.Integer(), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_index_jobs_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_index_jobs_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_index_jobs_snapshot_id_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_jobs")),
    )
    op.create_index(op.f("ix_index_jobs_org_id"), "index_jobs", ["org_id"], unique=False)
    op.create_index(
        "ix_index_jobs_repo_id_created_at", "index_jobs", ["repo_id", "created_at"], unique=False
    )
    op.create_index(
        "uq_index_jobs_one_active_per_repo",
        "index_jobs",
        ["repo_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    for table in ("snapshots", "index_jobs"):
        for statement in enable_org_rls_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in ("index_jobs", "snapshots"):
        for statement in disable_org_rls_sql(table):
            op.execute(statement)
    op.drop_index(
        "uq_index_jobs_one_active_per_repo",
        table_name="index_jobs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_index_jobs_repo_id_created_at", table_name="index_jobs")
    op.drop_index(op.f("ix_index_jobs_org_id"), table_name="index_jobs")
    op.drop_table("index_jobs")
    op.drop_index(op.f("ix_snapshots_org_id"), table_name="snapshots")
    op.drop_table("snapshots")
    for enum_type in reversed(ENUMS):
        enum_type.drop(op.get_bind(), checkfirst=True)
