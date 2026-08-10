"""Schemas for the seeker document portfolio (PRD §3.8)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.core.visa_types import OptionalVisaType
from app.models.booking import BookingStatus
from app.models.seeker_document import DocumentCategory, SeekerDocumentStatus

CustomerDocumentsRowStatus = Literal["pending", "completed", "rejected"]
ChecklistItemStatus = Literal["approved", "under_review", "rejected", "missing"]
DocumentCommentAuthorRole = Literal["seeker", "advisor", "admin"]


class ClientSeekerBrief(BaseModel):
    """Seeker identity for the advisor client-documents detail header."""

    seeker_id: uuid.UUID
    seeker_name: str | None
    seeker_email: str
    seeker_profile_photo_url: str | None


class SeekerDocumentCreate(BaseModel):
    """Reference to a file already uploaded via ``POST /uploads``
    (``category=seeker_document``)."""

    file_key: str = Field(min_length=1, max_length=500)
    file_name: str = Field(min_length=1, max_length=255)
    file_size_bytes: int = Field(ge=1)
    content_type: str = Field(default="application/octet-stream", max_length=100)
    category: DocumentCategory
    document_name: str = Field(min_length=1, max_length=255)
    expires_at: date | None = None
    visa_type: OptionalVisaType = None


class SeekerDocumentUpdate(BaseModel):
    """Patch expiry / visa scope / display name without re-uploading."""

    document_name: str | None = Field(default=None, min_length=1, max_length=255)
    expires_at: date | None = None
    clear_expires_at: bool = False
    visa_type: OptionalVisaType = None
    clear_visa_type: bool = False


class SeekerDocumentStatusUpdate(BaseModel):
    status: SeekerDocumentStatus


class SeekerDocumentRead(BaseModel):
    id: uuid.UUID
    seeker_id: uuid.UUID
    category: DocumentCategory
    document_name: str
    file_url: str
    file_size_bytes: int | None
    content_type: str
    status: SeekerDocumentStatus
    expires_at: date | None
    visa_type: OptionalVisaType
    reviewed_at: datetime | None
    reviewed_by: uuid.UUID | None
    created_at: datetime
    comments_count: int = 0


class DocumentChecklistItem(BaseModel):
    category: DocumentCategory
    label: str
    status: ChecklistItemStatus
    document_id: uuid.UUID | None = None


class DocumentPortfolioSummary(BaseModel):
    """Overview cards + required-doc checklist for the seeker Documents page."""

    total: int
    approved: int
    under_review: int
    missing: int
    rejected: int
    progress_percent: int
    checklist: list[DocumentChecklistItem]


class CustomerDocumentsRowRead(BaseModel):
    """One row on the advisor "Documents of customers" table.

    Scoped to a booking (appointment) + that seeker's document portfolio.
    """

    booking_id: uuid.UUID
    appointment_id: str
    seeker_id: uuid.UUID
    seeker_name: str | None
    seeker_email: str
    seeker_profile_photo_url: str | None
    service_type: str
    booking_status: BookingStatus
    documents_count: int
    # pending = zero docs, any under_review, or mixed approved/rejected;
    # completed = all approved; rejected = all rejected (none under_review).
    documents_status: CustomerDocumentsRowStatus
    updated_at: datetime


class DocumentCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class DocumentCommentRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    author_id: uuid.UUID
    author_name: str | None
    author_role: DocumentCommentAuthorRole | None = None
    body: str
    created_at: datetime
