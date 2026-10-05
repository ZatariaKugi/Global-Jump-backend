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
    """Publishable key plus the admin rules the client needs to render copy.

    Fee amounts themselves come from ``BookingRead.platform_fee_usd``; a client must
    not recompute them (a fixed commission cannot be derived from a rate).
    """

    publishable_key: str | None
    commission_type: Literal["percent", "fixed"]
    commission_value: float
    seeker_reschedule_window_hours: int
    advisor_cancellation_window_hours: int
    platform_fee_refund_behavior: Literal["retained", "refunded"]
    # Legacy field for the current web client: percent / 100, or null for a fixed fee.
    platform_commission_rate: float | None = None


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


class TransactionRefundRead(BaseModel):
    """One refund attempt on a payment (EPIC 04 refund ledger)."""

    id: uuid.UUID
    transaction_id: uuid.UUID
    kind: Literal["advisor_cancel", "admin_full", "admin_partial", "expiry", "rejection"]
    status: Literal["pending", "refunded", "reversed", "failed"]
    refund_to_seeker_usd: float
    advisor_reversed_usd: float
    platform_fee_refunded_usd: float
    fee_policy_refunded: bool
    stripe_refund_id: str | None
    stripe_reversal_id: str | None
    reason: str | None
    initiated_by: uuid.UUID | None
    last_error: str | None
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
    service_id: uuid.UUID | None
    name: str
    scheduled_start: datetime
    invoice_id: str | None = None
    display_id: str | None = None
    display_status: PaymentDisplayStatus = "pending"
    seeker_email: str | None = None
    advisor_email: str | None = None
    seeker_phone: str | None = None
    advisor_phone: str | None = None
    seeker_country: str | None = None
    # Fully-qualified (presigned S3 or absolute) URLs for Next.js <Image>; null → initials.
    seeker_photo_url: str | None = None
    advisor_photo_url: str | None = None
    # Card details from Stripe (null for pending/failed where no charge exists).
    card_brand: str | None = None
    card_last4: str | None = None
    # Transfer lifecycle — exposed so FE can hide Refund before transfer / when window closed.
    transfer_status: str | None = None
    # Free-text admin note on the payment.
    admin_note: str | None = None
    # EPIC 04: how the money moved and what has been pulled back so far.
    charge_model: Literal["separate_transfer", "destination"] = "separate_transfer"
    application_fee_usd: float = 0.0
    advisor_reversed_usd: float = 0.0
    platform_fee_refunded_usd: float = 0.0
    refunds: list[TransactionRefundRead] = []


class TransactionAdvisorRead(TransactionRead):
    """Enriched row for the advisor's customer-payments / earnings history."""

    seeker_id: uuid.UUID
    seeker_name: str | None
    seeker_email: str | None = None
    service_id: uuid.UUID | None
    name: str
    scheduled_start: datetime
    appointment_id: str | None = None
    invoice_id: str | None = None
    display_id: str | None = None
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
    display_id: str | None = None
    advisor_id: uuid.UUID
    advisor_name: str | None
    advisor_email: str | None
    advisor_photo_url: str | None
    service_id: uuid.UUID | None
    name: str
    created_at: datetime
    amount_usd: float
    total_amount: float  # same as amount_usd — grand total charged (FE column name)
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
    # Same definition as Financial Analytics' "Advisor Earnings" card, deliberately --
    # see platform_payment_summary() for why it does not share the row set of the four
    # figures above.
    total_advisor_earnings_usd: float


class AdvisorConnectStatus(BaseModel):
    stripe_account_id: str | None
    charges_enabled: bool
    payouts_enabled: bool = False
    onboarding_complete: bool
    onboarding_url: str | None = None
    account_type: str | None = None


class StripeDashboardLink(BaseModel):
    """``POST /advisors/me/stripe-connect/dashboard`` — open the advisor's Stripe dashboard."""

    dashboard_url: str


class AdvisorEarnings(BaseModel):
    """Advisor earnings. The advisor share is paid inside each charge (destination
    charge), so there is no separate balance to withdraw."""

    total_earned_usd: float
    total_commission_paid_usd: float
    transactions: list[TransactionRead]


class RefundCreate(BaseModel):
    """Admin refund. ``amount_usd`` absent means a full refund of what is left."""

    reason: str | None = None
    amount_usd: float | None = Field(default=None, gt=0)
    # Pull the advisor's share back from the connected account.
    reverse_advisor_share: bool = True
    # Return the platform fee too (full refund defaults to yes, partial to no).
    refund_platform_fee: bool | None = None


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
    from_phone: str | None = None
    to_name: str | None
    to_email: str
    to_address: str | None = None
    to_phone: str | None = None
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
