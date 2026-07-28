"""Regulatory / policy updates surfaced on the advisor dashboard.

A flat, admin-curated feed of immigration-policy changes shown to every advisor
(no per-advisor targeting) — the "Regulatory Updates" card and its "See all"
list on ``/advisor/dashboard``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class RegulatoryUpdate(BaseModel):
    __tablename__ = "regulatory_updates"

    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    region_label: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
