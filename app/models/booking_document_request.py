"""Advisor requests for specific documents from a seeker, tied to a booking (PRD §3.8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class DocumentRequestStatus(StrEnum):
    requested = "requested"
    fulfilled = "fulfilled"


class BookingDocumentRequest(BaseModel):
    __tablename__ = "booking_document_requests"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[DocumentRequestStatus] = mapped_column(
        SAEnum(DocumentRequestStatus, name="document_request_status"),
        default=DocumentRequestStatus.requested,
        nullable=False,
    )

    # Populated on fulfillment.
    file_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
