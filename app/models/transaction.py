"""Payment transaction record — one row per booking payment (PRD §3.10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class TransactionStatus(StrEnum):
    pending = "pending"  # checkout session created, payment not yet confirmed
    succeeded = "succeeded"  # payment confirmed, funds transferred to advisor
    partially_refunded = "partially_refunded"  # refund issued for less than the full amount
    refunded = "refunded"  # full refund issued
    failed = "failed"  # checkout session expired or payment failed


class Transaction(BaseModel):
    __tablename__ = "transactions"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    stripe_checkout_session_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    stripe_charge_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    commission_rate: Mapped[float] = mapped_column(Float, nullable=False)
    commission_usd: Mapped[float] = mapped_column(Float, nullable=False)
    tax_rate: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.08")
    tax_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    advisor_payout_usd: Mapped[float] = mapped_column(Float, nullable=False)
    payment_method: Mapped[str] = mapped_column(
        String(50), default="card", server_default="card", nullable=False
    )

    status: Mapped[TransactionStatus] = mapped_column(
        SAEnum(TransactionStatus, name="transaction_status"),
        default=TransactionStatus.pending,
        nullable=False,
    )

    # Assigned once the transaction succeeds (see payment_service._next_invoice_number) —
    # only paid transactions get invoiced.
    invoice_number: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)

    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    refund_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    refunded_amount_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
