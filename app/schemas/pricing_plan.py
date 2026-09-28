"""Pricing plan schemas (EPIC 04, PAY-112 / 113 / 114)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

AudienceLiteral = Literal["seeker", "advisor"]
StatusLiteral = Literal["draft", "active", "inactive"]
IntervalLiteral = Literal["month"]
KindLiteral = Literal["enforceable", "display"]
ValueTypeLiteral = Literal["number", "bool", "text"]


class PlanFeatureInput(BaseModel):
    kind: KindLiteral
    feature_key: str | None = Field(default=None, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    value_type: ValueTypeLiteral = "text"
    # number: "15" or "" / "unlimited"; bool: "true" / "false"; text: any label
    value: str = Field(default="", max_length=64)
    is_enabled: bool = True
    display_order: int = 0

    @model_validator(mode="after")
    def _shape(self) -> PlanFeatureInput:
        if self.kind == "enforceable" and not self.feature_key:
            raise ValueError("enforceable features need a feature_key from the catalog")
        if self.kind == "display":
            self.feature_key = None
        if (
            self.value_type == "number"
            and self.value not in ("", "unlimited")
            and not self.value.isdigit()
        ):
            raise ValueError("a number feature value must be a whole number, '' or 'unlimited'")
        if self.value_type == "bool" and self.value not in ("true", "false", ""):
            raise ValueError("a bool feature value must be 'true' or 'false'")
        return self


class PlanFeatureRead(BaseModel):
    id: uuid.UUID
    kind: KindLiteral
    feature_key: str | None
    label: str
    value_type: ValueTypeLiteral
    value: str
    is_enabled: bool
    display_order: int


class PricingPlanCreate(BaseModel):
    audience: AudienceLiteral
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    tagline: str | None = Field(default=None, max_length=200)
    price_usd: Decimal = Field(ge=0, decimal_places=2)
    billing_interval: IntervalLiteral = "month"
    is_highlighted: bool = False
    features: list[PlanFeatureInput] = Field(default_factory=list)


class PricingPlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    tagline: str | None = Field(default=None, max_length=200)
    price_usd: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    is_highlighted: bool | None = None
    features: list[PlanFeatureInput] | None = None


class PricingPlanPublicRead(BaseModel):
    """What seekers and advisors see. Never carries Stripe ids."""

    id: uuid.UUID
    audience: AudienceLiteral
    name: str
    description: str | None
    tagline: str | None
    price_usd: Decimal
    currency: str
    billing_interval: IntervalLiteral
    is_highlighted: bool
    is_free: bool
    features: list[PlanFeatureRead]


class PricingPlanAdminRead(PricingPlanPublicRead):
    status: StatusLiteral
    stripe_product_id: str | None
    stripe_price_id: str | None
    price_version: int
    sync_error: str | None
    last_synced_at: datetime | None
    active_subscribers: int = 0
    created_at: datetime
    updated_at: datetime


class FeatureCatalogRead(BaseModel):
    key: str
    label: str
    module: str
    audience: str
    limit_type: ValueTypeLiteral
    display_order: int
