"""Advisor credential documents submitted for admin verification."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import Date, DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class DocumentType(StrEnum):
    immigration_license = "immigration_license"
    bar_membership = "bar_membership"
    certification = "certification"
    government_id = "government_id"
    other = "other"


class CredentialStatus(StrEnum):
    pending = "pending"
    verified = "verified"
    rejected = "rejected"
    expired = "expired"


class AdvisorCredential(BaseModel):
    __tablename__ = "advisor_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_type: Mapped[DocumentType] = mapped_column(
        SAEnum(DocumentType, name="document_type"), nullable=False
    )
    document_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[CredentialStatus] = mapped_column(
        SAEnum(CredentialStatus, name="credential_status"),
        default=CredentialStatus.pending,
        nullable=False,
    )
    admin_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    @property
    def document_format(self) -> str:
        """Derived from the stored file's extension (save_upload always names
        files {uuid}{suffix}) — not a DB column."""
        suffix = Path(self.file_url).suffix.lstrip(".").upper()
        return suffix or "FILE"
