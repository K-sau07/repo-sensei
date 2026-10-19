# pyright: basic
"""Database roles for tenancy: the RLS-enforced app role and the BYPASSRLS system role.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18

Migrations run as the schema owner. The application connects as ``sensei_app`` (never bypasses
row-level security); ``sensei_system`` bypasses RLS and is reserved for explicitly named
maintenance tasks. Roles are cluster-wide, so they are created if missing and their passwords are
kept in sync with settings; downgrade revokes this database's grants but leaves the roles in place
(other databases in the cluster may still use them).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from sensei_core.config import get_settings
from sensei_core.db.tenancy import APP_ROLE, SYSTEM_ROLE

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = (APP_ROLE, SYSTEM_ROLE)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ensure_role(name: str, password: str, *, bypass_rls: bool) -> None:
    attributes = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE " + (
        "BYPASSRLS" if bypass_rls else "NOBYPASSRLS"
    )
    # DDL cannot take bind parameters: the role name is a constant and the password is escaped.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN
                CREATE ROLE {name} {attributes} PASSWORD {_literal(password)};
            ELSE
                ALTER ROLE {name} {attributes} PASSWORD {_literal(password)};
            END IF;
        END
        $$;
        """  # noqa: S608 - ruff reads the directive at the end of multi-line strings
    )


def upgrade() -> None:
    database = get_settings().database
    _ensure_role(APP_ROLE, database.app_password.get_secret_value(), bypass_rls=False)
    _ensure_role(SYSTEM_ROLE, database.system_password.get_secret_value(), bypass_rls=True)

    roles = ", ".join(ROLES)
    op.execute(
        f"DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO {roles}', "
        "current_database()); END $$;"
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {roles}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {roles}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {roles}")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {roles}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {roles}"
    )
    # The migration bookkeeping table is the owner's business only.
    op.execute(f"REVOKE ALL ON alembic_version FROM {roles}")


def downgrade() -> None:
    roles = ", ".join(ROLES)
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {roles}"  # noqa: S608
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {roles}"
    )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {roles}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {roles}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {roles}")
    op.execute(
        f"DO $$ BEGIN EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {roles}', "
        "current_database()); END $$;"
    )
