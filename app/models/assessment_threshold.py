"""Admin-configurable score-to-tier cutoffs for the eligibility assessment
engine (PRD §3.4 AI Engine Management). Replaces the hardcoded 80/60/40
breakpoints previously baked into ``assessment_service.tier_for_score``."""

from __future__ import annotations

from sqlalchemy import Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class AssessmentThreshold(BaseModel):
    __tablename__ = "assessment_thresholds"
    __table_args__ = (UniqueConstraint("country_code", "visa_type"),)

    # NULL = global default (same convention as AssessmentQuestion/EligibilityRule).
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    visa_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # UI-labeled "Eligibility Score" / "Moderate Score" / "Not Eligible Score".
    highly_eligible_min: Mapped[float] = mapped_column(Float, nullable=False)
    likely_eligible_min: Mapped[float] = mapped_column(Float, nullable=False)
    borderline_min: Mapped[float] = mapped_column(Float, nullable=False)

    is_active: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
