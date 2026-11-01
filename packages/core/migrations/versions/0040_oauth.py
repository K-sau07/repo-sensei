# pyright: basic
"""OAuth 2.1 (7.2): ``oauth_clients`` (registered, or known by their metadata document's URL),
``oauth_grants`` (authorization requests and their one-use codes), ``oauth_device_codes`` (the
CLI's device flow) and ``oauth_issued_tokens`` (access and rotating refresh tokens, each refresh
family revocable at once), and the ``oauth`` audit actor. Credentials like browser sessions:
only hashes of the secrets, found by hash, not behind org row-level security.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-01 19:15:57.175438+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_actor_type ADD VALUE IF NOT EXISTS 'oauth'")
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("dynamic", "metadata_document", name="oauth_client_kind"),
            nullable=False,
        ),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column("client_uri", sa.Text(), nullable=True),
        sa.Column("redirect_uris", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fresh_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_clients")),
        sa.UniqueConstraint("client_id", name=op.f("uq_oauth_clients_client_id")),
    )
    op.create_table(
        "oauth_device_codes",
        sa.Column("device_code_hash", sa.LargeBinary(), nullable=False),
        sa.Column("user_code", sa.String(length=16), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "denied", "used", name="oauth_grant_status"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.id"],
            name=op.f("fk_oauth_device_codes_client_id_oauth_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_oauth_device_codes_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_oauth_device_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_device_codes")),
        sa.UniqueConstraint(
            "device_code_hash", name=op.f("uq_oauth_device_codes_device_code_hash")
        ),
        sa.UniqueConstraint("user_code", name=op.f("uq_oauth_device_codes_user_code")),
    )
    op.create_index(
        op.f("ix_oauth_device_codes_client_id"), "oauth_device_codes", ["client_id"], unique=False
    )
    op.create_table(
        "oauth_grants",
        sa.Column("request_hash", sa.LargeBinary(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "approved",
                "denied",
                "used",
                name="oauth_grant_status",
                create_type=False,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("code_hash", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.id"],
            name=op.f("fk_oauth_grants_client_id_oauth_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_oauth_grants_org_id_orgs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_oauth_grants_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_grants")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_oauth_grants_code_hash")),
        sa.UniqueConstraint("request_hash", name=op.f("uq_oauth_grants_request_hash")),
    )
    op.create_index(op.f("ix_oauth_grants_client_id"), "oauth_grants", ["client_id"], unique=False)
    op.create_table(
        "oauth_issued_tokens",
        sa.Column("kind", sa.Enum("access", "refresh", name="oauth_token_kind"), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["oauth_clients.id"],
            name=op.f("fk_oauth_issued_tokens_client_id_oauth_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
            name=op.f("fk_oauth_issued_tokens_org_id_orgs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_oauth_issued_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_issued_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_oauth_issued_tokens_token_hash")),
    )
    op.create_index(
        op.f("ix_oauth_issued_tokens_client_id"), "oauth_issued_tokens", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_oauth_issued_tokens_family_id"), "oauth_issued_tokens", ["family_id"], unique=False
    )
    op.create_index(
        op.f("ix_oauth_issued_tokens_user_id"), "oauth_issued_tokens", ["user_id"], unique=False
    )


def downgrade() -> None:
    # The 'oauth' audit actor stays: Postgres can't drop a value from an enum.
    op.drop_index(op.f("ix_oauth_issued_tokens_user_id"), table_name="oauth_issued_tokens")
    op.drop_index(op.f("ix_oauth_issued_tokens_family_id"), table_name="oauth_issued_tokens")
    op.drop_index(op.f("ix_oauth_issued_tokens_client_id"), table_name="oauth_issued_tokens")
    op.drop_table("oauth_issued_tokens")
    op.drop_index(op.f("ix_oauth_grants_client_id"), table_name="oauth_grants")
    op.drop_table("oauth_grants")
    op.drop_index(op.f("ix_oauth_device_codes_client_id"), table_name="oauth_device_codes")
    op.drop_table("oauth_device_codes")
    op.drop_table("oauth_clients")
    for name in ("oauth_token_kind", "oauth_grant_status", "oauth_client_kind"):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
