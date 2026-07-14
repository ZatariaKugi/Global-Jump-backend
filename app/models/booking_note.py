"""Advisor notes attached to a specific consultation booking."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel


class BookingNote(BaseModel):
    __tablename__ = "booking_notes"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Either body or at least one attachment must be present (enforced in the service).
    body: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    attachments: Mapped[list[BookingNoteAttachment]] = relationship(
        back_populates="note",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="BookingNoteAttachment.id",
    )


class BookingNoteAttachment(Base):
    """One row per file attached to a booking note."""

    __tablename__ = "booking_note_attachments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    note_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("booking_notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)

    note: Mapped[BookingNote] = relationship(back_populates="attachments")
