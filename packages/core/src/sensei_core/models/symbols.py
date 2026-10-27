"""Code nodes: the definitions of a snapshot (modules, classes, functions, components, ...).

One row per symbol, keyed by its stable symbol key within the snapshot (see
:mod:`sensei_engine.symbols.keys`). ``owner_file_id`` is the file whose extraction produced the
row: when that file changes, everything it owns is deleted and extracted again.

The graph (3.1, :mod:`sensei_core.models.graph`) adds nodes of its own kinds (an endpoint, a
table, a package: :class:`~sensei_core.models.graph.NodeKind`), some of them in no file (no
``file_id``, no owner). Readers of definitions keep to
:data:`~sensei_core.models.graph.SYMBOL_KINDS`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin
from sensei_core.db.tenancy import OrgScopedMixin
from sensei_core.models.graph import NodeKind

_KINDS = ", ".join(f"'{kind.value}'" for kind in NodeKind)


class CodeNode(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "nodes"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "symbol_key"),
        UniqueConstraint("id", "org_id"),  # for chunks' (symbol_id, org_id) foreign key
        Index("ix_nodes_repo_id", "repo_id"),  # deleting a repository cascades here
        # Lookup by name, each within a snapshot: exact and prefix on the lowered name
        # (text_pattern_ops serves LIKE 'abc%' whatever the collation); a qualified name's
        # ending as a prefix of it reversed; contains and fuzzy by trigrams (btree_gin puts the
        # snapshot in the same index).
        Index(
            "ix_nodes_snapshot_lower_name",
            "snapshot_id",
            text("lower(name) text_pattern_ops"),
        ),
        Index(
            "ix_nodes_snapshot_reverse_qualified",
            "snapshot_id",
            text("reverse(lower(qualified_name)) text_pattern_ops"),
        ),
        Index(
            "ix_nodes_snapshot_name_trgm",
            "snapshot_id",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # The symbols of a file (under the cursor; deleted with their owner).
        Index("ix_nodes_file_id_lines", "file_id", "start_line"),
        Index("ix_nodes_owner_file_id", "owner_file_id"),
        CheckConstraint(f"kind IN ({_KINDS})", name="kind"),
        # With org_id: a node can't point at another org's repository, snapshot or file.
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "org_id"], ["snapshots.id", "snapshots.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["file_id", "org_id"], ["files.id", "files.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["owner_file_id", "org_id"], ["files.id", "files.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    snapshot_id: Mapped[uuid.UUID] = mapped_column()
    # A NodeKind: a definition's (finer: attrs["subkind"]) or a graph fact's.
    kind: Mapped[str] = mapped_column(String(32))
    symbol_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    qualified_name: Mapped[str] = mapped_column(Text)
    file_id: Mapped[uuid.UUID | None] = mapped_column()  # None: in no file (a package, ...)
    start_line: Mapped[int] = mapped_column(Integer)  # 1-based, inclusive
    end_line: Mapped[int] = mapped_column(Integer)
    start_col: Mapped[int] = mapped_column(Integer)  # characters, 0-based
    end_col: Mapped[int] = mapped_column(Integer)
    lang: Mapped[str] = mapped_column(String(16))  # the file's language id
    signature: Mapped[str | None] = mapped_column(Text)
    docstring: Mapped[str | None] = mapped_column(Text)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    content_hash: Mapped[bytes] = mapped_column(LargeBinary)  # SHA-256 of the definition
    owner_file_id: Mapped[uuid.UUID | None] = mapped_column()  # None: owned by no file
