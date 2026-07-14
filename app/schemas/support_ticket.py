"""Schemas for admin support tickets (PRD §4.6 Support & Moderation)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.support_ticket import TicketCategory, TicketPriority, TicketStatus


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
    user_id: uuid.UUID
    user_name: str | None
    subject: str
    description: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus
    preferred_contact_at: datetime | None
    resolved_at: datetime | None
    resolved_by: uuid.UUID | None
    created_at: datetime
