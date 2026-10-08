"""Payment schemas (PRD §3.10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, computed_field

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
    # Stripe Tax adds tax at checkout from the seeker's billing address (QA 2026-10-08):
    # the pay sheet says "tax is added by Stripe"; the amount is only known once paid.
    automatic_tax_enabled: bool = False


class TransactionRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    booking_id: uuid.UUID
    # What the seeker paid: consultation + tax.
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
    # QA 2026-10-08: which tax rule applied, and Stripe's processing fee (borne by
    # the advisor).
    tax_label: str | None = None
    tax_country: str | None = None
    tax_jurisdiction: str | None = None
    stripe_fee_usd: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def consultation_usd(self) -> float:
        """The consultation price: what the seeker paid less the tax on it."""
        return round(float(self.amount_usd) - float(self.tax_usd or 0), 2)


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
    # Human name of the rule that applied, for the ledger (QA REF-089).
    refund_rule: Literal["admin_keeps_fee", "seeker_keeps_fee", "platform_fault", "admin"] = "admin"
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
    # QA 2026-10-08: whether Stripe's fee was pulled back from the advisor, and what the
    # advisor is left with after the fee and any reversal.
    stripe_fee_recovered: bool = False
    stripe_fee_reversal_id: str | None = None
    stripe_balance_transaction_id: str | None = None
    advisor_net_usd: float = 0.0


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
    # Net = gross share - Stripe fee - anything reversed (QA 2026-10-08, section 5).
    net_amount_usd: float = 0.0
    advisor_gross_usd: float = 0.0
    advisor_reversed_usd: float = 0.0


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
    # Collected and held for the tax authority; never platform revenue.
    total_tax_usd: float
    # Same definition as Financial Analytics' "Advisor Earnings" card, deliberately --
    # see platform_payment_summary() for why it does not share the row set of the four
    # figures above.
    total_advisor_earnings_usd: float
    # Stripe's processing fees over every charged row, borne by the advisors.
    total_stripe_fee_usd: float = 0.0


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
    charge), so there is no separate balance to withdraw.

    The five-line breakdown of QA 2026-10-08 section 5 plus Total Refunded (section 17):
    gross revenue (consultations, tax excluded) = platform fee + advisor gross;
    advisor net = advisor gross - Stripe fee - reversals.
    """

    # Net: what the advisor actually kept. (Historical name.)
    total_earned_usd: float
    # Platform commission across the same rows. (Historical name; same as platform fee.)
    total_commission_paid_usd: float
    transactions: list[TransactionRead]
    total_gross_revenue_usd: float = 0.0
    total_platform_fee_usd: float = 0.0
    total_advisor_gross_usd: float = 0.0
    total_stripe_fee_usd: float = 0.0
    total_refunded_usd: float = 0.0


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
    tax_rate_percent: float = 0.0
    tax_label: str | None = None
    total_usd: float
    status: TransactionStatus
    display_status: PaymentDisplayStatus
    refunded_amount_usd: float | None
    refunded_at: datetime | None
    refund_reason: str | None
    terms: str | None = None
