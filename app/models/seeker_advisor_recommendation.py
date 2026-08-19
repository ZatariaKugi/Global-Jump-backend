"""Persisted seeker-facing AI advisor recommendations (Find Advisor cache)."""

from __future__ import annotations

import uuid

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class SeekerAdvisorRecommendation(BaseModel):
    """Profile-based ranked advisor suggestion for Find Advisor.

    Regenerated as a full set (replace-all) from seeker profile intent when
    onboarding completes or ``GET /advisors?recommended=true`` finds no rows
    for the current destination/visa. AI Assessment never writes this table.
    """

    __tablename__ = "seeker_advisor_recommendations"
    __table_args__ = (UniqueConstraint("seeker_id", "advisor_id"),)

    seeker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="SET NULL"), nullable=True, index=True
    )

    destination_country: Mapped[str] = mapped_column(String(2), nullable=False)
    visa_type: Mapped[str] = mapped_column(String(50), nullable=False)
    context_source: Mapped[str] = mapped_column(String(20), nullable=False)

    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    rule_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_reasons: Mapped[str] = mapped_column(String(1000), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
