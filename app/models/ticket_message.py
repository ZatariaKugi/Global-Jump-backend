"""Messages within a support ticket's conversation thread (PRD §4.6)."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel


class TicketMessage(BaseModel):
    __tablename__ = "ticket_messages"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Either body or at least one attachment must be present (enforced in the service).
    body: Mapped[str | None] = mapped_column(String(5000), nullable=True)

    attachments: Mapped[list[TicketMessageAttachment]] = relationship(
        back_populates="ticket_message",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TicketMessageAttachment.id",
    )


class TicketMessageAttachment(Base):
    """One row per file attached to a ticket message."""

    __tablename__ = "ticket_message_attachments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    ticket_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)

    ticket_message: Mapped[TicketMessage] = relationship(back_populates="attachments")
