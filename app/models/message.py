"""Threaded messages within a conversation, with attachments (PRD §3.7)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel
from app.models.review import ModerationStatus


class Message(BaseModel):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Either body or at least one attachment must be present (enforced in the service).
    body: Mapped[str | None] = mapped_column(String(5000), nullable=True)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Sender-initiated soft delete — distinct from moderation_status, which is an
    # admin action against someone else's content.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    # Reuses the "moderation_status" enum/table introduced for reviews (PRD §3.9).
    moderation_status: Mapped[ModerationStatus] = mapped_column(
        SAEnum(ModerationStatus, name="moderation_status"),
        default=ModerationStatus.visible,
        nullable=False,
    )
    flag_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    flagged_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    attachments: Mapped[list[MessageAttachment]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MessageAttachment.id",
    )


class MessageAttachment(Base):
    """One row per file attached to a message."""

    __tablename__ = "message_attachments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)

    message: Mapped[Message] = relationship(back_populates="attachments")
