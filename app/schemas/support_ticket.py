"""Schemas for admin support tickets (PRD §4.6 Support & Moderation)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, model_validator

from app.models.support_ticket import TicketCategory, TicketPriority, TicketStatus
from app.models.user import UserRole
from app.schemas.ticket_message import TicketAttachmentRead, TicketMessageAttachmentRef


def _coerce_ticket_category(value: Any) -> Any:
    """Map FE 'payment' → billing; otherwise pass through."""
    if isinstance(value, str) and value.strip().lower() == "payment":
        return TicketCategory.billing
    return value


CoercedTicketCategory = Annotated[TicketCategory, BeforeValidator(_coerce_ticket_category)]


class TicketCreate(BaseModel):
    """Create a support ticket.

    Provide ``user_id`` **or** ``user_email`` (exactly one). Attachments are
    uploaded first via ``POST /uploads`` (``category=ticket_attachment``), then
    passed as ``attachments[]`` — they become the opening admin message.
    """

    user_id: uuid.UUID | None = None
    user_email: str | None = Field(default=None, max_length=320)
    subject: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    category: CoercedTicketCategory
    priority: TicketPriority = TicketPriority.medium
    preferred_contact_at: datetime | None = None
    assigned_to: uuid.UUID | None = None
    internal_notes: str | None = Field(default=None, max_length=2000)
    attachments: list[TicketMessageAttachmentRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_user_ref(self) -> TicketCreate:
        has_id = self.user_id is not None
        has_email = bool(self.user_email and self.user_email.strip())
        if has_id == has_email:
            raise ValueError("Provide exactly one of user_id or user_email")
        if self.user_email:
            self.user_email = self.user_email.strip().lower()
        return self


class TicketUpdate(BaseModel):
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    category: CoercedTicketCategory | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    preferred_contact_at: datetime | None = None
    assigned_to: uuid.UUID | None = None
    internal_notes: str | None = Field(default=None, max_length=2000)


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
    internal_notes: str | None
    resolved_at: datetime | None
    resolved_by: uuid.UUID | None
    assigned_to: uuid.UUID | None
    assigned_to_name: str | None
    total_responses: int
    last_response_at: datetime | None
    attachments: list[TicketAttachmentRead]
    created_at: datetime
    updated_at: datetime
