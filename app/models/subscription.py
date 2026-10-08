"""Subscriptions and metered usage (EPIC 04, PAY-110 / PAY-111).

A ``Subscription`` mirrors one Stripe subscription for one user. Status changes only
through webhooks. ``SubscriptionUsage`` counts number-type plan features per period so
limits such as "15 assessments per month" can be enforced; the period is the Stripe
billing period for subscribers and the UTC calendar month for free-plan users.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel


class SubscriptionStatus(StrEnum):
    incomplete = "incomplete"
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"
    unpaid = "unpaid"
    expired = "expired"


class Subscription(BaseModel):
    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pricing_plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    stripe_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, name="subscription_status"),
        nullable=False,
        default=SubscriptionStatus.incomplete,
        index=True,
    )
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Entitlements stay open until this instant (period end plus the grace days).
    access_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A plan change waiting for the period end (downgrade, or the free plan = cancel).
    # Stripe performs the switch through a subscription schedule; the rollover event
    # clears these three and moves ``plan_id``.
    scheduled_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pricing_plans.id", ondelete="SET NULL"), nullable=True
    )
    scheduled_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stripe_schedule_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Which version of the plan the subscriber bought; the features below are that
    # version's perks, so an admin edit never reaches a live subscriber mid-period.
    plan_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # The monthly price the subscriber signed up for; the plan's price may move on.
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    # Card summary from the customer's default payment method; never the PAN.
    payment_method_brand: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payment_method_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    payment_method_exp_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_method_exp_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    features: Mapped[list[SubscriptionFeature]] = relationship(
        "SubscriptionFeature", cascade="all, delete-orphan", lazy="selectin"
    )


class SubscriptionFeature(Base):
    """The subscriber's perks, copied from the plan when the subscription starts,
    changes plan, or renews. ``entitlement_service`` reads these for a live
    subscription, never the plan's current rows."""

    __tablename__ = "subscription_features"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)  # number | bool
    value: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class SubscriptionInvoice(Base):
    __tablename__ = "subscription_invoices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which plan this charge was for, snapshotted when the invoice is written.
    # `subscriptions.plan_id` is the plan the subscriber is on *now*, so resolving
    # through it would re-attribute every past payment the moment anyone upgrades.
    # Nullable + SET NULL: a retired plan keeps its revenue history.
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pricing_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stripe_invoice_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    stripe_invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # paid | open | void | ...
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hosted_invoice_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    invoice_pdf_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SubscriptionUsage(Base):
    __tablename__ = "subscription_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "feature_key", "period_start", name="uq_usage_user_key_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
