"""Advisor payout requests — a manual withdrawal ledger on top of accrued
transaction earnings (PRD §3.10). Distinct from Stripe Connect's automatic
per-transaction transfers: this covers funds sitting in the platform account
that an advisor pulls out on demand."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class PayoutStatus(StrEnum):
    pending = "pending"
    completed = "completed"
    rejected = "rejected"


class PayoutMethod(StrEnum):
    bank_transfer = "bank_transfer"
    paypal = "paypal"
    stripe = "stripe"


class PayoutRequest(BaseModel):
    __tablename__ = "payout_requests"

    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[PayoutMethod] = mapped_column(
        SAEnum(PayoutMethod, name="payout_method"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    processing_fee_usd: Mapped[float] = mapped_column(Float, nullable=False)
    net_amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[PayoutStatus] = mapped_column(
        SAEnum(PayoutStatus, name="payout_status"),
        default=PayoutStatus.pending,
        nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
