"""The code graph (3.1): typed, confidence-scored edges between a snapshot's nodes.

Nodes are the ``nodes`` table's rows (:class:`~sensei_core.models.symbols.CodeNode`): the
definitions the symbols stage extracts (:data:`SYMBOL_KINDS`), and the facts the graph stages
add (an HTTP call, an endpoint, an ORM model, a table, a package: :class:`NodeKind`). A graph
node may belong to no file (a package, an external symbol, a table only migrations name): its
``file_id`` and ``owner_file_id`` are then empty.

An edge says one node relates to another (:class:`EdgeType`), how the extractor knows
(:class:`Provenance`, which sets its default ``confidence``: 1.0 for a compiler, 0.4 for a name
that's unique, ...), and where (the line that proves it). Every edge is **owned** by the file
whose extraction produced it, or by none (a snapshot-wide fact, such as a call matched to an
endpoint): when the file changes, everything it owns is deleted and extracted again, so no
stale fact outlives its evidence. An edge goes with either end's node.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import (
    REAL,
    CheckConstraint,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin
from sensei_core.db.tenancy import OrgScopedMixin


def _values(enum_type: type[enum.StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class NodeKind(enum.StrEnum):
    # Definitions (the symbols stage, 2.4).
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    COMPONENT = "component"  # React
    HOOK = "hook"
    VARIABLE = "variable"
    # Structure.
    SERVICE = "service"
    PACKAGE = "package"
    EXTERNAL_SYMBOL = "external_symbol"  # what an import names outside the repository
    # Boundaries between layers.
    HTTP_CLIENT_CALL = "http_client_call"
    API_ENDPOINT = "api_endpoint"  # attrs: method, path_norm, framework
    GRAPHQL_OPERATION = "graphql_operation"
    GRAPHQL_FIELD = "graphql_field"
    RPC_METHOD = "rpc_method"
    TOPIC = "topic"
    # Data.
    ORM_MODEL = "orm_model"
    TABLE = "table"
    COLUMN = "column"
    SQL_STATEMENT = "sql_statement"
    # Other.
    ENV_VAR = "env_var"
    CONFIG = "config"
    TEST = "test"


# What the symbols stage writes (and symbol lookup, hovers and summaries read).
SYMBOL_KINDS: tuple[str, ...] = (
    NodeKind.MODULE,
    NodeKind.CLASS,
    NodeKind.FUNCTION,
    NodeKind.METHOD,
    NodeKind.COMPONENT,
    NodeKind.HOOK,
    NodeKind.VARIABLE,
)
GRAPH_KINDS: tuple[str, ...] = tuple(kind for kind in NodeKind if kind not in SYMBOL_KINDS)


class EdgeType(enum.StrEnum):
    CONTAINS = "contains"  # file or class -> what it holds
    DEFINES = "defines"
    IMPORTS = "imports"  # module -> module or symbol
    CALLS = "calls"  # function -> function
    REFERENCES = "references"  # function -> symbol
    INHERITS = "inherits"  # class -> class
    IMPLEMENTS = "implements"
    RENDERS = "renders"  # component -> component
    USES_HOOK = "uses_hook"  # component -> hook
    INJECTS = "injects"  # handler -> dependency (FastAPI Depends)
    ISSUES_REQUEST = "issues_request"  # function -> http_client_call
    TARGETS = "targets"  # http_client_call -> api_endpoint
    HANDLED_BY = "handled_by"  # api_endpoint -> function
    RESOLVES = "resolves"  # graphql_field -> function
    PUBLISHES = "publishes"  # function -> topic
    CONSUMES = "consumes"
    MAPS_TO = "maps_to"  # orm_model -> table, field -> column
    READS = "reads"  # function -> orm_model or table
    WRITES = "writes"
    TESTS = "tests"  # test -> function


class Provenance(enum.StrEnum):
    """How an edge is known, from a compiler's word to a model's guess."""

    SCIP = "scip"
    LSP = "lsp"
    RUNTIME = "runtime"  # introspected (a running app's routes)
    CONTRACT = "contract"  # an OpenAPI operationId, a proto, a GraphQL schema
    IMPORT_RESOLVED = "import_resolved"
    RECEIVER_HEURISTIC = "receiver_heuristic"  # self.x, an annotated type, a constructor
    URL_LITERAL = "url_literal"  # every literal path segment matched
    NAME_UNIQUE = "name_unique"
    NAME_AMBIGUOUS = "name_ambiguous"  # one of k candidates
    LLM_INFERRED = "llm_inferred"  # flagged: kept apart, left out of blast radius by default


class CodeEdge(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "edges"
    __table_args__ = (
        # One edge per evidence line (one with no line counts once).
        Index(
            "uq_edges_snapshot_src_dst_type_line",
            "snapshot_id",
            "src_id",
            "dst_id",
            "type",
            "evidence_line",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_edges_src_id_type", "src_id", "type"),
        Index("ix_edges_dst_id_type", "dst_id", "type"),
        Index("ix_edges_owner_file_id", "owner_file_id"),
        Index("ix_edges_evidence_file_id", "evidence_file_id"),  # deleting a file cascades here
        Index("ix_edges_repo_id", "repo_id"),  # deleting a repository cascades here
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        # With org_id: an edge can't point at another org's nodes, snapshot or files.
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "org_id"], ["snapshots.id", "snapshots.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["src_id", "org_id"], ["nodes.id", "nodes.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["dst_id", "org_id"], ["nodes.id", "nodes.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["evidence_file_id", "org_id"], ["files.id", "files.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["owner_file_id", "org_id"], ["files.id", "files.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    snapshot_id: Mapped[uuid.UUID] = mapped_column()
    src_id: Mapped[uuid.UUID] = mapped_column()
    dst_id: Mapped[uuid.UUID] = mapped_column()
    type: Mapped[EdgeType] = mapped_column(
        Enum(EdgeType, name="edge_type", values_callable=_values)
    )
    confidence: Mapped[float] = mapped_column(REAL)
    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="edge_provenance", values_callable=_values)
    )
    evidence_file_id: Mapped[uuid.UUID | None] = mapped_column()
    evidence_line: Mapped[int | None] = mapped_column(Integer)  # 1-based
    owner_file_id: Mapped[uuid.UUID | None] = mapped_column()  # None: snapshot-wide
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
