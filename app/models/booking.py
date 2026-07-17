"""Consultation bookings between seekers and advisors (PRD §3.6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel

# Human-readable appointment IDs (Consultation Requests List) start near the mock style.
APPOINTMENT_NUMBER_START = 3_520_000_000


class BookingStatus(StrEnum):
    pending = "pending"
    confirmed = "confirmed"
    completed = "completed"
    cancelled = "cancelled"
    rejected = "rejected"
    no_show = "no_show"


class PaymentStatus(StrEnum):
    """Stub until the payment epic (#12) wires real transactions."""

    unpaid = "unpaid"
    paid = "paid"
    refunded = "refunded"


class Booking(BaseModel):
    __tablename__ = "bookings"

    seeker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Human-readable Appointment ID shown in the Consultations list (e.g. "3520145678").
    appointment_number: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    # Service snapshot — copied from the advisor's offering at booking time so
    # later price/duration changes don't rewrite history.
    service_type: Mapped[str] = mapped_column(String(100), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)

    # Always UTC.
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[BookingStatus] = mapped_column(
        SAEnum(BookingStatus, name="booking_status"),
        default=BookingStatus.confirmed,
        nullable=False,
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status"),
        default=PaymentStatus.unpaid,
        nullable=False,
    )

    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    seeker_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Set when the advisor chooses "Deal Later" on a pending request.
    deal_later_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_important: Mapped[bool] = mapped_column(
        default=False, server_default="false", nullable=False
    )
    interpreter_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interpreter_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interpreter_language: Mapped[str | None] = mapped_column(String(100), nullable=True)
