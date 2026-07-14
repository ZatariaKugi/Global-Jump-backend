"""Payment schemas (PRD §3.10)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.transaction import TransactionStatus


class CheckoutCreate(BaseModel):
    booking_id: uuid.UUID


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


class TransactionRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    booking_id: uuid.UUID
    amount_usd: float
    commission_rate: float
    commission_usd: float
    tax_rate: float
    tax_usd: float
    advisor_payout_usd: float
    payment_method: str
    invoice_number: int | None
    status: TransactionStatus
    stripe_payment_intent_id: str | None
    refunded_at: datetime | None
    refund_reason: str | None
    created_at: datetime


class TransactionAdminRead(TransactionRead):
    refunded_by: uuid.UUID | None
    refunded_amount_usd: float | None
    stripe_checkout_session_id: str
    stripe_charge_id: str | None


class TransactionFinanceRead(TransactionAdminRead):
    """Enriched row for the admin Finance Management list/detail views."""

    seeker_id: uuid.UUID
    seeker_name: str | None
    advisor_id: uuid.UUID
    advisor_name: str | None
    service_type: str
    scheduled_start: datetime


class TransactionAdvisorRead(TransactionRead):
    """Enriched row for the advisor's "Payment of customers" list."""

    seeker_id: uuid.UUID
    seeker_name: str | None
    service_type: str
    scheduled_start: datetime


class AdvisorConnectStatus(BaseModel):
    stripe_account_id: str | None
    charges_enabled: bool
    onboarding_complete: bool
    onboarding_url: str | None = None


class AdvisorEarnings(BaseModel):
    total_earned_usd: float
    total_commission_paid_usd: float
    available_balance_usd: float
    transactions: list[TransactionRead]


class RefundCreate(BaseModel):
    reason: str | None = None
    amount_usd: float | None = Field(default=None, gt=0)


class InvoiceLineItem(BaseModel):
    description: str
    quantity: int
    unit_price_usd: float
    total_usd: float


class InvoiceRead(BaseModel):
    invoice_number: str
    issued_date: datetime
    transaction_id: uuid.UUID
    booking_id: uuid.UUID
    from_name: str
    to_name: str | None
    to_email: str
    line_items: list[InvoiceLineItem]
    total_usd: float
    status: TransactionStatus
    refunded_amount_usd: float | None
    refunded_at: datetime | None
    refund_reason: str | None
