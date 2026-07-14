"""AI-matched customer leads for advisors — inverse of advisor_matching_service (PRD §3.4.3)."""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class AdvisorLeadStatus(StrEnum):
    new = "new"
    viewed = "viewed"
    contacted = "contacted"
    dismissed = "dismissed"


class AdvisorLead(BaseModel):
    __tablename__ = "advisor_leads"
    __table_args__ = (UniqueConstraint("seeker_id", "advisor_id", "assessment_id"),)

    seeker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    match_reasons: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[AdvisorLeadStatus] = mapped_column(
        SAEnum(AdvisorLeadStatus, name="advisor_lead_status"),
        default=AdvisorLeadStatus.new,
        nullable=False,
    )
