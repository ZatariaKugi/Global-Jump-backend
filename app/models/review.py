"""Post-session ratings and reviews (PRD §3.9)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class ModerationStatus(StrEnum):
    visible = "visible"
    flagged = "flagged"  # reported, awaiting admin review; still publicly visible
    removed = "removed"  # admin removed; hidden from public listings


class Review(BaseModel):
    __tablename__ = "reviews"

    # One review per booking (PRD: only one review per booking).
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    seeker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # 1–5 star dimensions (PRD §3.9).
    rating_expertise: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_communication: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_professionalism: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_value: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_overall: Mapped[float] = mapped_column(Float, nullable=False)  # avg of the four

    text: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Verified badge: review came from a confirmed paying seeker.
    is_verified: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Advisor may post a public reply; editable/deletable via PATCH/DELETE.
    advisor_response: Mapped[str | None] = mapped_column(String(500), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Moderation (PRD: admin moderation queue for flagged reviews).
    moderation_status: Mapped[ModerationStatus] = mapped_column(
        SAEnum(ModerationStatus, name="moderation_status"),
        default=ModerationStatus.visible,
        nullable=False,
    )
    flag_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    flagged_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
