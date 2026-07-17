"""Seeker bookmarks of advisors (Bookmarked list screen)."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class AdvisorBookmark(BaseModel):
    __tablename__ = "advisor_bookmarks"
    __table_args__ = (UniqueConstraint("seeker_id", "advisor_id"),)

    seeker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
