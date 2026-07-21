"""Schemas for consultation bookings (PRD §3.6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.advisor_profile import AdvisorServiceType
from app.models.booking import BookingStatus, PaymentStatus
from app.schemas.booking_document_request import DocumentRequestRead
from app.schemas.booking_note import BookingNoteRead

# List sort: leading ``-`` = descending (default matches prior hardcoded order).
BookingSort = Literal["scheduled_start", "-scheduled_start"]


class BookingCreate(BaseModel):
    advisor_id: uuid.UUID
    service_type: AdvisorServiceType
    scheduled_start: datetime
    seeker_note: str | None = Field(default=None, max_length=1000)


class AdvisorBookingCreate(BaseModel):
    """Advisor books a consultation directly for one of their existing clients."""

    seeker_id: uuid.UUID
    service_type: AdvisorServiceType
    scheduled_start: datetime
    seeker_note: str | None = Field(default=None, max_length=1000)


class ClientRead(BaseModel):
    id: uuid.UUID
    full_name: str | None
    email: str


class BookingReschedule(BaseModel):
    scheduled_start: datetime


class BookingCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class BookingReject(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class BookingImportantUpdate(BaseModel):
    is_important: bool


class BookingInterpreterUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    contact: str | None = Field(default=None, max_length=255)
    language: str | None = Field(default=None, max_length=100)


class BookingRead(BaseModel):
    id: uuid.UUID
    appointment_id: str
    seeker_id: uuid.UUID
    advisor_id: uuid.UUID
    seeker_name: str | None
    seeker_email: str | None
    advisor_name: str | None
    advisor_email: str | None
    advisor_profile_photo_url: str | None
    service_type: str
    duration_minutes: int
    # ``price_usd`` is the total charge; fee split uses PLATFORM_COMMISSION_RATE.
    advisor_fee_usd: float
    platform_fee_usd: float
    price_usd: float
    scheduled_start: datetime
    scheduled_end: datetime
    status: BookingStatus
    payment_status: PaymentStatus
    cancellation_reason: str | None
    seeker_note: str | None
    deal_later_at: datetime | None
    is_important: bool
    interpreter_name: str | None
    interpreter_contact: str | None
    interpreter_language: str | None
    created_at: datetime


class BookingHistoryRead(BaseModel):
    """Consultation History screen — booking summary plus notes and document requests."""

    id: uuid.UUID
    appointment_id: str
    seeker_id: uuid.UUID
    advisor_id: uuid.UUID
    seeker_name: str | None
    seeker_email: str | None
    advisor_name: str | None
    service_type: str
    scheduled_start: datetime
    scheduled_end: datetime
    status: BookingStatus
    seeker_note: str | None
    is_important: bool
    deal_later_at: datetime | None
    notes: list[BookingNoteRead]
    document_requests: list[DocumentRequestRead]
    created_at: datetime


class BookingAttachmentRead(BaseModel):
    id: uuid.UUID
    title: str
    format: str
    size: int


class BookingMeetingRead(BaseModel):
    label: str
    time_range: str
    date: str


class BookingAiSuggestionRead(BaseModel):
    id: uuid.UUID
    message: str


class BookingDetailsRead(BaseModel):
    """View Booking Details drawer — required + optional presentation fields."""

    appointment_id: str
    seeker_name: str | None
    service_type: str
    scheduled_start: datetime
    duration_minutes: int
    amount_paid: float
    description: str
    attachments: list[BookingAttachmentRead] = Field(default_factory=list)
    meeting: BookingMeetingRead | None = None
    ai_suggestions: list[BookingAiSuggestionRead] = Field(default_factory=list)
