"""Schemas for advisor document requests on a booking (PRD §3.8)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.booking_document_request import DocumentRequestStatus


class DocumentRequestCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)


class DocumentRequestFulfill(BaseModel):
    """Reference to a file already uploaded via ``POST /uploads``
    (``category=booking_document``)."""

    file_key: str = Field(min_length=1, max_length=500)
    file_name: str = Field(min_length=1, max_length=255)
    file_size_bytes: int = Field(ge=1)
    content_type: str = Field(default="application/octet-stream", max_length=100)


class DocumentRequestRead(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    requested_by: uuid.UUID
    description: str
    status: DocumentRequestStatus
    file_url: str | None
    file_name: str | None
    file_size: int | None
    content_type: str | None
    fulfilled_at: datetime | None
    created_at: datetime
