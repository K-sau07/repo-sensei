"""Background job bookkeeping."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin


class JobFailure(IdMixin, Base):
    """A task that failed for good (out of retries, or a non-retryable error): the dead letters.

    A system table read by staff through the system role; ``args`` are stored redacted."""

    __tablename__ = "job_failures"

    task: Mapped[str] = mapped_column(String(200), index=True)
    task_id: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(JSONB)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orgs.id", ondelete="SET NULL"), index=True
    )
    repo_id: Mapped[uuid.UUID | None]
    error_type: Mapped[str] = mapped_column(String(200))
    error: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
