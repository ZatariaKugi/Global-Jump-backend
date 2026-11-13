"""Admin-managed pricing plans synced to Stripe (EPIC 04, PAY-112 / 113 / 114).

Everything on a plan is admin data: the plan itself can be activated or deactivated,
its price and text changed, and each feature added, removed, switched on or off, or
given a new value. The only thing code owns is the catalog of *enforceable* feature
keys (one per project module) and the gate behind each key.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel


class PlanAudience(StrEnum):
    seeker = "seeker"
    advisor = "advisor"


class PlanStatus(StrEnum):
    draft = "draft"  # Stripe objects not (fully) created; hidden from users
    active = "active"
    inactive = "inactive"  # archived on Stripe; existing subscribers keep billing


class BillingInterval(StrEnum):
    month = "month"


class FeatureKind(StrEnum):
    enforceable = "enforceable"  # key from the catalog; the code has a gate for it
    display = "display"  # free text shown on the card only


class FeatureValueType(StrEnum):
    number = "number"  # a per-period limit; "" or "unlimited" means no limit
    bool = "bool"  # "true" / "false"
    text = "text"  # display-only value such as "Limited"


class PlanFeatureCatalog(Base):
    """Seeded list of enforceable keys. Admins pick from it; they cannot add to it."""

    __tablename__ = "plan_feature_catalog"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    audience: Mapped[str] = mapped_column(String(16), nullable=False)  # seeker | advisor | both
    limit_type: Mapped[str] = mapped_column(String(16), nullable=False)  # number | bool | text
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PricingPlan(BaseModel):
    __tablename__ = "pricing_plans"

    audience: Mapped[PlanAudience] = mapped_column(
        SAEnum(PlanAudience, name="plan_audience"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    tagline: Mapped[str | None] = mapped_column(String(200), nullable=True)
    price_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="usd", server_default="usd"
    )
    billing_interval: Mapped[BillingInterval] = mapped_column(
        SAEnum(BillingInterval, name="billing_interval"),
        nullable=False,
        default=BillingInterval.month,
        server_default=BillingInterval.month.value,
    )
    status: Mapped[PlanStatus] = mapped_column(
        SAEnum(PlanStatus, name="plan_status"),
        nullable=False,
        default=PlanStatus.draft,
        server_default=PlanStatus.draft.value,
        index=True,
    )
    is_highlighted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The audience's designated free plan: every user of that audience without a paid
    # subscription sits on it. One per audience; only a $0 plan may carry it.
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    stripe_product_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    price_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sync_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    features: Mapped[list[PricingPlanFeature]] = relationship(
        "PricingPlanFeature",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PricingPlanFeature.display_order",
    )


class PricingPlanFeature(Base):
    __tablename__ = "pricing_plan_features"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pricing_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[FeatureKind] = mapped_column(
        SAEnum(FeatureKind, name="feature_kind"), nullable=False
    )
    # Catalog key for enforceable features; null for display features.
    feature_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    value_type: Mapped[FeatureValueType] = mapped_column(
        SAEnum(FeatureValueType, name="feature_value_type"), nullable=False
    )
    value: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
