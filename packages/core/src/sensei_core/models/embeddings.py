"""The embedding cache: one vector per content hash, model and size, shared by every org.

Content-addressed and holding no text (a SHA-256 and a vector), so it needs no org scoping: the
same chunk in two repositories, or in two snapshots of one, is embedded once. Rows are never
changed (a model's vector for a content hash doesn't change): the app role may only read and add
them; the system role may also delete (a model retired).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, LargeBinary, String, func
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

    content_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)  # "provider/model"
    dims: Mapped[int] = mapped_column(Integer, primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(HalfVector())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
