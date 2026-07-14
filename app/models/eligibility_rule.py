"""Admin-configurable eligibility scoring rules (PRD §3.4 AI Engine Management).

Management surface only for now — not yet consumed by ``assessment_service``'s
scoring formula. See the admin-panel plan notes for why: the rule categories
shown in the mockup (e.g. "Age", "Other") don't map cleanly onto the existing
per-question ``QuestionCategory`` weighting, and guessing at that mapping risks
silently changing the already-tested scoring engine.
"""

from __future__ import annotations

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class EligibilityRule(BaseModel):
    __tablename__ = "eligibility_rules"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # NULL = applies to all countries / visa types (same convention as AssessmentQuestion).
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    visa_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    points: Mapped[float] = mapped_column(Float, nullable=False)
    weightage_pct: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
