"""A/B test variants for assessment questionnaire experiments (AI Engine)."""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class AssessmentAbVariant(BaseModel):
    """Named experiment arm (A/B/C/D) optionally scoped by country/visa."""

    __tablename__ = "assessment_ab_variants"

    label: Mapped[str] = mapped_column(String(8), nullable=False)  # A, B, C, D
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Card body copy on the A/B Testing admin panel (the experiment question).
    question: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    visa_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
