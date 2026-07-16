"""Payment schemas (PRD §3.10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.transaction import TransactionStatus

PaymentDisplayStatus = Literal["paid", "pending", "refunded", "failed"]
InvoicePerspective = Literal["seeker", "advisor", "admin"]


class CheckoutCreate(BaseModel):
    booking_id: uuid.UUID


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


class PaymentConfigRead(BaseModel):
    publishable_key: str | None


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
    invoice_id: str | None = None
    display_status: PaymentDisplayStatus = "pending"
    seeker_email: str | None = None
    advisor_email: str | None = None
    seeker_country: str | None = None


class TransactionAdvisorRead(TransactionRead):
    """Enriched row for the advisor's customer-payments / earnings history."""

    seeker_id: uuid.UUID
    seeker_name: str | None
    service_type: str
    scheduled_start: datetime
    appointment_id: str | None = None
    invoice_id: str | None = None
    display_status: PaymentDisplayStatus = "pending"
    seeker_photo_url: str | None = None
    platform_fee_usd: float = 0.0
    consultant_fee_usd: float = 0.0
    net_amount_usd: float = 0.0


class SeekerPaymentRead(BaseModel):
    """Visa-seeker Payments list/detail row (image copy.png)."""

    id: uuid.UUID
    booking_id: uuid.UUID
    invoice_id: str | None
    advisor_id: uuid.UUID
    advisor_name: str | None
    advisor_email: str | None
    advisor_photo_url: str | None
    service_type: str
    created_at: datetime
    platform_fee_usd: float
    consultant_fee_usd: float
    amount_usd: float
    status: TransactionStatus
    display_status: PaymentDisplayStatus
    payment_method: str
    stripe_payment_intent_id: str | None
    refunded_amount_usd: float | None = None
    refunded_at: datetime | None = None
    refund_reason: str | None = None


class SeekerPaymentSummaryRead(BaseModel):
    total_paid_usd: float
    pending_amount_usd: float
    refund_amount_usd: float
    last_transaction_usd: float | None


class PaymentSummaryRead(BaseModel):
    """Admin / platform-wide payment summary cards."""

    total_paid_usd: float
    total_refunded_usd: float
    total_commission_usd: float
    total_tax_usd: float


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
    invoice_id: str
    issued_date: datetime
    due_date: datetime
    transaction_id: uuid.UUID
    booking_id: uuid.UUID
    from_name: str
    from_address: str | None = None
    to_name: str | None
    to_email: str
    to_address: str | None = None
    line_items: list[InvoiceLineItem]
    subtotal_usd: float
    tax_usd: float
    total_usd: float
    status: TransactionStatus
    display_status: PaymentDisplayStatus
    refunded_amount_usd: float | None
    refunded_at: datetime | None
    refund_reason: str | None
    terms: str | None = None
