"""The glossary of a snapshot: the domain terms its summaries use (2.8), each with the symbols
described by it. Search reads it to bridge the words a question uses and the names the code
uses ("invoice" -> ``BillingDocument``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Float, ForeignKeyConstraint, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin
from sensei_core.db.tenancy import OrgScopedMixin


class GlossaryTerm(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "glossary_terms"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "term"),
        Index("ix_glossary_terms_repo_id", "repo_id"),  # deleting a repository cascades here
        # With org_id: a term can't point at another org's repository or snapshot.
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "org_id"], ["snapshots.id", "snapshots.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    snapshot_id: Mapped[uuid.UUID] = mapped_column()
    term: Mapped[str] = mapped_column(Text)  # lower case, single spaces
    # The nodes whose summaries name the term (ids of ``nodes`` rows in the same snapshot).
    symbol_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    weight: Mapped[float] = mapped_column(Float)  # how many symbols name it
