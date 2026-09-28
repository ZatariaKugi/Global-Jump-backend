"""Refund ledger for consultation payments (EPIC 04, PAY-104 / PAY-105).

One row per refund attempt. A refund on a destination charge is two Stripe calls
(a Refund to the seeker, then a Transfer reversal from the advisor); the row records
both ids so a crash between the calls can be resumed with the same idempotency keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RefundKind(StrEnum):
    advisor_cancel = "advisor_cancel"  # document §3.2: advisor share back, fee per policy
    admin_full = "admin_full"
    admin_partial = "admin_partial"
    expiry = "expiry"  # advisor never accepted: platform fault, full refund
    rejection = "rejection"  # advisor rejected: platform fault, full refund


class RefundStatus(StrEnum):
    pending = "pending"  # row written, no Stripe call succeeded yet
    refunded = "refunded"  # seeker refund created, advisor reversal still due
    reversed = "reversed"  # complete
    failed = "failed"  # a Stripe call failed; retryable with the same keys


class TransactionRefund(Base):
    __tablename__ = "transaction_refunds"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[RefundKind] = mapped_column(SAEnum(RefundKind, name="refund_kind"), nullable=False)
    refund_to_seeker_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    advisor_reversed_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    platform_fee_refunded_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), server_default="0"
    )
    # Snapshot of the admin "platform fee refund behaviour" that applied.
    fee_policy_refunded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    stripe_refund_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_reversal_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[RefundStatus] = mapped_column(
        SAEnum(RefundStatus, name="refund_status"),
        nullable=False,
        default=RefundStatus.pending,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
