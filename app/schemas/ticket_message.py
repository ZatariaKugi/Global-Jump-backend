"""Schemas for a support ticket's conversation thread (PRD §4.6)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TicketMessageAttachmentRef(BaseModel):
    """Reference to a file already uploaded via ``POST /uploads``
    (``category=ticket_attachment``).  Include one entry per file to attach."""

    file_key: str = Field(min_length=1, max_length=500)
    file_name: str = Field(min_length=1, max_length=255)
    file_size_bytes: int = Field(ge=1)
    content_type: str = Field(default="application/octet-stream", max_length=100)


class TicketMessageSend(BaseModel):
    body: str | None = Field(default=None, max_length=5000)
    attachments: list[TicketMessageAttachmentRef] = Field(default_factory=list)


class TicketAttachmentRead(BaseModel):
    id: uuid.UUID
    file_url: str
    file_name: str
    file_size: int
    content_type: str


class TicketMessageRead(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    sender_id: uuid.UUID
    sender_name: str | None
    body: str | None
    attachments: list[TicketAttachmentRead]
    created_at: datetime
