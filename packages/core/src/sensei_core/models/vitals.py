"""Web vitals (10.11): how fast the app's pages are for the people using them, measured in
their browsers. One row per measure of a page view (LCP, INP or CLS), with the page's route as a
pattern (``/[org]/[repo]/ask``: no repository's or person's name), kept 30 days."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin
from sensei_core.db.tenancy import OrgScopedMixin

VITALS = ("LCP", "INP", "CLS")
VITALS_RETENTION_DAYS = 30


class WebVital(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "web_vitals"
    __table_args__ = (Index("ix_web_vitals_org_created", "org_id", "created_at"),)

    name: Mapped[str] = mapped_column(String(8))  # one of VITALS
    value: Mapped[float] = mapped_column(Float)  # ms (LCP, INP); unitless (CLS)
    route: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
