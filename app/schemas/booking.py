"""Schemas for consultation bookings (PRD §3.6)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.booking import BookingStatus, PaymentStatus


class BookingCreate(BaseModel):
    advisor_id: uuid.UUID
    service_type: str = Field(min_length=1, max_length=100)
    scheduled_start: datetime
    seeker_note: str | None = Field(default=None, max_length=1000)


class AdvisorBookingCreate(BaseModel):
    """Advisor books a consultation directly for one of their existing clients."""

    seeker_id: uuid.UUID
    service_type: str = Field(min_length=1, max_length=100)
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
    seeker_id: uuid.UUID
    advisor_id: uuid.UUID
    seeker_name: str | None
    advisor_name: str | None
    service_type: str
    duration_minutes: int
    price_usd: float
    scheduled_start: datetime
    scheduled_end: datetime
    status: BookingStatus
    payment_status: PaymentStatus
    cancellation_reason: str | None
    seeker_note: str | None
    is_important: bool
    interpreter_name: str | None
    interpreter_contact: str | None
    interpreter_language: str | None
    created_at: datetime
