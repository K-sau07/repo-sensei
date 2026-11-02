# pyright: basic
"""Fix runs (8.3, audit): one open run per issue (a partial unique index: two deliveries at once
can't both start one), and an approval's run in its own org (a composite foreign key).

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-02 09:19:32.882305+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OPEN = (
    "source_ref IS NOT NULL AND status NOT IN ('succeeded', 'not_reproduced', 'blocked', "
    "'failed', 'rejected', 'cancelled')"
)
FK = "fk_fix_approvals_fix_run_id_fix_runs"


def upgrade() -> None:
    # The (id, org_id) key first: the approvals' foreign key points at it.
    op.create_unique_constraint(op.f("uq_fix_runs_id_org_id"), "fix_runs", ["id", "org_id"])
    op.drop_constraint(op.f(FK), "fix_approvals", type_="foreignkey")
    op.create_foreign_key(
        op.f(FK), "fix_approvals", "fix_runs", ["fix_run_id", "org_id"], ["id", "org_id"],
        ondelete="CASCADE",
    )  # fmt: skip
    op.create_index(
        "uq_fix_runs_open_ref",
        "fix_runs",
        ["repo_id", "source", "source_ref"],
        unique=True,
        postgresql_where=sa.text(OPEN),
    )


def downgrade() -> None:
    op.drop_index("uq_fix_runs_open_ref", table_name="fix_runs", postgresql_where=sa.text(OPEN))
    op.drop_constraint(op.f(FK), "fix_approvals", type_="foreignkey")
    op.create_foreign_key(
        op.f(FK), "fix_approvals", "fix_runs", ["fix_run_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_constraint(op.f("uq_fix_runs_id_org_id"), "fix_runs", type_="unique")
