"""Append-only timeline of a transaction's lifecycle steps (PRD §4.5 Finance Management)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TransactionEventType(StrEnum):
    initiated = "initiated"
    authorized = "authorized"
    completed = "completed"
    invoice_generated = "invoice_generated"
    receipt_sent = "receipt_sent"
    refunded = "refunded"
    failed = "failed"
    closed = "closed"


class TransactionEvent(Base):
    """One row per lifecycle step a transaction has actually passed through."""

    __tablename__ = "transaction_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[TransactionEventType] = mapped_column(
        SAEnum(TransactionEventType, name="transaction_event_type"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
