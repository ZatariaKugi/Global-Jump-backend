"""Schemas for advisor notes attached to a booking."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class BookingNoteAttachmentRef(BaseModel):
    """Reference to a file already uploaded via ``POST /uploads``
    (``category=booking_note``). Include one entry per file to attach."""

    file_key: str = Field(min_length=1, max_length=500)
    file_name: str = Field(min_length=1, max_length=255)
    file_size_bytes: int = Field(ge=1)
    content_type: str = Field(default="application/octet-stream", max_length=100)


class BookingNoteCreate(BaseModel):
    body: str | None = Field(default=None, max_length=2000)
    attachments: list[BookingNoteAttachmentRef] = Field(default_factory=list)


class BookingNoteAttachmentRead(BaseModel):
    id: uuid.UUID
    file_url: str
    file_name: str
    file_size: int
    content_type: str


class BookingNoteRead(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    author_id: uuid.UUID
    author_name: str | None
    body: str | None
    attachments: list[BookingNoteAttachmentRead]
    created_at: datetime
