"""Conversation threads between a seeker and their booked advisor (PRD §3.7)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class Conversation(BaseModel):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("seeker_id", "advisor_id"),)

    seeker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
