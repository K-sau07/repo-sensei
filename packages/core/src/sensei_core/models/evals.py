"""Stored evaluation runs (written by the ``sensei-eval`` CLI, read on the staff dashboard)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin


class EvalRun(IdMixin, Base):
    """One run of a golden suite against one retriever at one repository commit.

    A system table with no org data: tooling writes it through the system role; the app role may
    only read it, and only staff routes do."""

    __tablename__ = "eval_runs"

    suite: Mapped[str] = mapped_column(String(200), index=True)
    suite_version: Mapped[int] = mapped_column(Integer)
    retriever: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    repo_sha: Mapped[str] = mapped_column(String(40))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    per_item: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    git_sha: Mapped[str | None] = mapped_column(String(40))
