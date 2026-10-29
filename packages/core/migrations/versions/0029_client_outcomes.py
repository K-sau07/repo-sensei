# pyright: basic
"""What the client does with a response (3.22): ``client_outcome`` nodes (a token stored, a page
navigated to, state set, an error shown) and ``produces`` edges from the function that does it.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-29 20:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# As of revision 0027, which this one extends.
NODE_KINDS = (
    "module", "class", "function", "method", "component", "hook", "variable",
    "service", "package", "external_symbol",
    "http_client_call", "api_endpoint", "graphql_operation", "graphql_field", "rpc_method",
    "topic", "orm_model", "table", "column", "sql_statement", "env_var", "config", "test",
)  # fmt: skip
EDGE_TYPES = (
    "contains", "defines", "imports", "calls", "references", "inherits", "implements",
    "renders", "uses_hook", "injects", "issues_request", "targets", "handled_by", "resolves",
    "publishes", "consumes", "maps_to", "reads", "writes", "tests",
)  # fmt: skip


def _kinds_check(kinds: Sequence[str]) -> None:
    op.drop_constraint(op.f("ck_nodes_kind"), "nodes", type_="check")
    listed = ", ".join(f"'{kind}'" for kind in kinds)
    op.create_check_constraint(op.f("ck_nodes_kind"), "nodes", f"kind IN ({listed})")


def upgrade() -> None:
    # A value added in this transaction can't be used in it: nothing here does.
    op.execute("ALTER TYPE edge_type ADD VALUE IF NOT EXISTS 'produces'")
    _kinds_check((*NODE_KINDS, "client_outcome"))


def downgrade() -> None:
    # Row-level security off, so a role it would filter fails here instead of deleting nothing.
    op.execute("SET LOCAL row_security = off")
    op.execute("DELETE FROM edges WHERE type = 'produces'")
    op.execute("DELETE FROM nodes WHERE kind = 'client_outcome'")
    _kinds_check(NODE_KINDS)
    # An enum value can't be dropped: the type is made again without it.
    values = ", ".join(f"'{value}'" for value in EDGE_TYPES)
    op.execute("ALTER TYPE edge_type RENAME TO edge_type_old")
    op.execute(f"CREATE TYPE edge_type AS ENUM ({values})")
    op.execute("ALTER TABLE edges ALTER COLUMN type TYPE edge_type USING type::text::edge_type")
    op.execute("DROP TYPE edge_type_old")
