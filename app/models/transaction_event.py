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
    transfer_scheduled = "transfer_scheduled"  # delayed payout hold armed
    transfer_completed = "transfer_completed"  # advisor payout transferred
    transfer_failed = "transfer_failed"  # transfer attempt failed (retryable)
    refund_requested = "refund_requested"  # EPIC 04 refund engine started
    transfer_reversed = "transfer_reversed"  # advisor share pulled back
    refund_failed = "refund_failed"  # a Stripe refund/reversal call failed
    stripe_fee_recovered = "stripe_fee_recovered"  # Stripe's fee reversed off the advisor


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
