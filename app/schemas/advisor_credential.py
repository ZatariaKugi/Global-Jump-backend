"""Schemas for advisor credential documents."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.advisor_credential import CredentialStatus, DocumentType


class AdvisorCredentialCreate(BaseModel):
    document_type: DocumentType
    document_name: str = Field(min_length=1, max_length=255)
    expiry_date: date | None = None


class AdvisorCredentialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    document_type: DocumentType
    document_name: str
    file_url: str
    document_format: str
    file_size_bytes: int | None
    expiry_date: date | None
    status: CredentialStatus
    admin_note: str | None
    verified_at: datetime | None
    verified_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AdvisorCredentialFromKey(BaseModel):
    """Used by ``POST /advisors/me/credentials`` after a file has been uploaded
    via ``POST /uploads``.  The ``file_key`` returned by the upload endpoint is
    passed here together with the credential metadata."""

    file_key: str = Field(min_length=1, max_length=500)
    document_type: DocumentType
    document_name: str = Field(min_length=1, max_length=255)
    expiry_date: date | None = None


class CredentialStatusUpdate(BaseModel):
    status: CredentialStatus
    admin_note: str | None = Field(default=None, max_length=1000)
