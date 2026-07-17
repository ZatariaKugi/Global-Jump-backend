"""Schemas for admin support tickets (PRD §4.6 Support & Moderation)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.support_ticket import TicketCategory, TicketPriority, TicketStatus
from app.models.user import UserRole


class TicketCreate(BaseModel):
    user_id: uuid.UUID
    subject: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    category: TicketCategory
    priority: TicketPriority = TicketPriority.medium
    preferred_contact_at: datetime | None = None


class TicketUpdate(BaseModel):
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    category: TicketCategory | None = None


class TicketRead(BaseModel):
    id: uuid.UUID
    ticket_number: str
    user_id: uuid.UUID
    user_name: str | None
    user_email: str | None
    user_type: UserRole | None
    avatar_url: str | None
    phone: str | None
    subject: str
    description: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus
    preferred_contact_at: datetime | None
    resolved_at: datetime | None
    resolved_by: uuid.UUID | None
    assigned_to: uuid.UUID | None
    assigned_to_name: str | None
    total_responses: int
    last_response_at: datetime | None
    created_at: datetime
    updated_at: datetime
