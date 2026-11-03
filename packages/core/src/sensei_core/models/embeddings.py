"""The embedding cache: one vector per org, content hash, model and size.

Within an org, the same chunk in two repositories, or in two snapshots of one, is embedded once.
Orgs never share rows: a vector can be partly inverted back to its text, and a shared cache would
tell one org which chunks another has already indexed (a hit costs nothing). Rows are never
changed (a model's vector for a content hash doesn't change): the app role may only read and add
them, in its own org; the system role may also delete (a model retired).

Public repositories' chunks (one imported by its public URL: the free tier, 10.1) are the
exception: their vectors are kept once for every org (``public_embedding_cache``), so a popular
repository is embedded once whoever imports it. Only those repositories write it, and read it:
their text is anyone's, so a vector of it tells nothing, and a hit tells only that some org has
indexed that public code, which the explore gallery (10.2) shows anyway.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base
from sensei_core.db.types import HalfVector

CONTENT_HASH_BYTES = 32  # SHA-256


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"
    __table_args__ = (
        CheckConstraint(f"octet_length(content_hash) = {CONTENT_HASH_BYTES}", name="hash_size"),
        CheckConstraint("vector_dims(embedding) = dims", name="embedding_dims"),
    )

    # Leads the primary key, which also serves as the org index (row-level security on it).
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)  # "provider/model"
    dims: Mapped[int] = mapped_column(Integer, primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(HalfVector())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublicEmbeddingCache(Base):
    """A public repository's chunk's vector, for every org (see the module)."""

    __tablename__ = "public_embedding_cache"
    __table_args__ = (
        CheckConstraint(f"octet_length(content_hash) = {CONTENT_HASH_BYTES}", name="hash_size"),
        CheckConstraint("vector_dims(embedding) = dims", name="embedding_dims"),
    )

    content_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    dims: Mapped[int] = mapped_column(Integer, primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(HalfVector())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
