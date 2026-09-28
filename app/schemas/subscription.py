"""Subscription and entitlement schemas (EPIC 04, PAY-110 / PAY-111)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

SubscriptionStatusLiteral = Literal[
    "incomplete", "trialing", "active", "past_due", "canceled", "unpaid", "expired"
]


class SubscriptionCheckoutCreate(BaseModel):
    plan_id: uuid.UUID


class SubscriptionCancel(BaseModel):
    at_period_end: bool = True


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


class ChangePlanPreviewRead(BaseModel):
    plan: SubscriptionPlanBrief
    amount_due_today_usd: Decimal | None
    note: str
    # "upgrade" is the only change allowed mid-period; "downgrade" is refused and
    # waits for the period to end; "cancel" is the free plan, which stops renewal.
    direction: Literal["upgrade", "downgrade", "cancel"] = "upgrade"


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
    status: SubscriptionStatusLiteral
    current_period_end: datetime | None
    cancel_at_period_end: bool
    stripe_subscription_id: str | None
    created_at: datetime


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
