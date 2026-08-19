"""Seeker document portfolio, reviewed by advisors and admins (PRD §3.8)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class DocumentCategory(StrEnum):
    passport = "passport"
    educational = "educational"
    finance = "finance"
    supporting = "supporting"
    other = "other"


class SeekerDocumentStatus(StrEnum):
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class SeekerDocument(BaseModel):
    __tablename__ = "seeker_documents"

    seeker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[DocumentCategory] = mapped_column(
        SAEnum(DocumentCategory, name="document_category"), nullable=False
    )
    document_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[SeekerDocumentStatus] = mapped_column(
        SAEnum(SeekerDocumentStatus, name="seeker_document_status"),
        default=SeekerDocumentStatus.under_review,
        nullable=False,
    )
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Optional visa scope (VisaType value); null = applies to any visa filter.
    visa_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # Seeker's last visit to this document's comments sheet. Null = never read.
    comments_last_read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SeekerDocumentComment(BaseModel):
    __tablename__ = "seeker_document_comments"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seeker_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(String(2000), nullable=False)
