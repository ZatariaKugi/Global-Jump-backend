"""Admin-configurable eligibility scoring rules (PRD §3.4 AI Engine Management).

Management surface only for now — not yet consumed by ``assessment_service``'s
scoring formula.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class EligibilityRuleCategory(StrEnum):
    age = "age"
    education = "education"
    work_experience = "work_experience"
    language_proficiency = "language_proficiency"
    other = "other"


class EligibilityRule(BaseModel):
    __tablename__ = "eligibility_rules"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    category: Mapped[EligibilityRuleCategory] = mapped_column(
        SAEnum(EligibilityRuleCategory, name="eligibility_rule_category"),
        default=EligibilityRuleCategory.other,
        server_default="other",
        nullable=False,
    )
    # NULL = applies to all countries / visa types (same convention as AssessmentQuestion).
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    visa_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    points: Mapped[float] = mapped_column(Float, nullable=False)
    weightage_pct: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
