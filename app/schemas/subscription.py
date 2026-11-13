"""Subscription and entitlement schemas (EPIC 04, PAY-110 / PAY-111)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SubscriptionStatusLiteral = Literal[
    "incomplete", "trialing", "active", "past_due", "canceled", "unpaid", "expired"
]


class SubscriptionCheckoutCreate(BaseModel):
    plan_id: uuid.UUID


class SubscriptionCancel(BaseModel):
    """Cancelling is a scheduled move to the free plan at the period end (PM,
    2026-10-08). There is no immediate cancel, so the body carries nothing."""

    model_config = ConfigDict(extra="forbid")


class SubscriptionChangePlan(BaseModel):
    plan_id: uuid.UUID


class SubscriptionPlanBrief(BaseModel):
    id: uuid.UUID
    name: str
    audience: Literal["seeker", "advisor"]
    price_usd: Decimal
    billing_interval: Literal["month"]


class PaymentMethodSummary(BaseModel):
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None


class SubscriptionInvoiceRead(BaseModel):
    id: uuid.UUID
    stripe_invoice_id: str
    invoice_number: str | None
    description: str | None
    amount_usd: Decimal
    status: str
    period_start: datetime | None
    period_end: datetime | None
    hosted_invoice_url: str | None
    invoice_pdf_url: str | None
    created_at: datetime


class InvoiceLineRead(BaseModel):
    description: str | None
    amount_usd: Decimal


class AdminInvoiceRead(SubscriptionInvoiceRead):
    """An invoice plus the lines that explain its total.

    A proration invoice's total ($767.56) means nothing beside a $1,000 plan price
    until you can see the credit and the charge it is made of.
    """

    lines: list[InvoiceLineRead] = []


class SubscriptionRead(BaseModel):
    id: uuid.UUID
    plan: SubscriptionPlanBrief
    status: SubscriptionStatusLiteral
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None
    access_until: datetime | None
    payment_method: PaymentMethodSummary | None
    latest_invoice: SubscriptionInvoiceRead | None = None
    # A downgrade or cancel waiting for the period end: the plan it moves to (the
    # free plan for a cancel) and when. Choosing the current plan again undoes it.
    scheduled_plan: SubscriptionPlanBrief | None = None
    scheduled_change_at: datetime | None = None


class ChangePlanPreviewRead(BaseModel):
    plan: SubscriptionPlanBrief
    amount_due_today_usd: Decimal | None
    note: str
    # "upgrade" is billed now and live at once; "downgrade" and "cancel" (the free
    # plan) are scheduled for ``effective_at``, the current period end.
    direction: Literal["upgrade", "downgrade", "cancel"] = "upgrade"
    effective_at: datetime | None = None


class PortalLinkRead(BaseModel):
    portal_url: str


class AdminSubscriptionRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_email: str
    user_name: str | None
    audience: Literal["seeker", "advisor"]
    plan_id: uuid.UUID
    plan_name: str
    price_usd: Decimal
    # What the subscriber has actually paid across every settled invoice — the plan's
    # monthly price says nothing about mid-period upgrades or failed renewals.
    total_charged_usd: Decimal = Decimal("0")
    # Which version of the plan the subscriber holds (QA BUG21); ``price_usd`` above
    # is what they signed up for, not necessarily the plan's price today.
    plan_version: int = 1
    status: SubscriptionStatusLiteral
    current_period_end: datetime | None
    cancel_at_period_end: bool
    scheduled_plan_name: str | None = None
    scheduled_change_at: datetime | None = None
    stripe_subscription_id: str | None
    created_at: datetime


class AdminSubscriptionPlanRevenueRead(BaseModel):
    """One row of the Revenue by Plan breakdown."""

    plan_id: uuid.UUID | None
    plan_name: str
    audience: Literal["seeker", "advisor"]
    revenue_usd: float
    change_pct: float | None
    subscriber_count: int


class AdminSubscriptionSummaryRead(BaseModel):
    """The cards above the Subscriptions Finance table.

    Counts are "right now" and carry no period. Revenue is money actually received
    inside the selected period, so the two halves always add to the total and each
    figure can be checked against the one beside it.
    """

    period: Literal["daily", "monthly", "yearly", "overall"]

    # Row 1 — where we stand today. Derived status, so these agree with the table.
    active_subscribers: int
    active_seekers: int
    active_advisors: int

    # Row 2 — money received in the period. `overall` has no period before it, so
    # every change is null rather than a meaningless 0%.
    total_revenue_usd: float
    total_revenue_change_pct: float | None
    advisor_revenue_usd: float
    advisor_revenue_change_pct: float | None
    seeker_revenue_usd: float
    seeker_revenue_change_pct: float | None

    # Row 3 — the same money, split by the plan each invoice was charged for.
    plans: list[AdminSubscriptionPlanRevenueRead]


class FeatureEntitlementRead(BaseModel):
    key: str
    label: str
    enabled: bool
    value_type: Literal["number", "bool", "text"]
    value: str
    limit: int | None = None
    used: int = 0
    remaining: int | None = None
    resets_at: datetime | None = None


class EntitlementsRead(BaseModel):
    plan_id: uuid.UUID | None
    plan_name: str | None
    subscription_status: SubscriptionStatusLiteral | None
    access_until: datetime | None
    unrestricted: bool = Field(
        description="True when no plan applies to this user (nothing configured yet)"
    )
    features: dict[str, FeatureEntitlementRead]
