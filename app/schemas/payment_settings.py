"""Admin payment settings schemas (EPIC 04, PAY-107)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CommissionTypeLiteral = Literal["percent", "fixed"]
FeeRefundBehaviorLiteral = Literal["retained", "refunded"]
# Stripe has exactly two environments; the admin picks one rather than us guessing.
StripeModeLiteral = Literal["test", "live"]

# Windows are hours before the scheduled start; 30 days is a generous ceiling.
MAX_WINDOW_HOURS = 720


class PaymentSettingsUpdate(BaseModel):
    seeker_reschedule_window_hours: int = Field(ge=0, le=MAX_WINDOW_HOURS)
    advisor_cancellation_window_hours: int = Field(ge=0, le=MAX_WINDOW_HOURS)
    commission_type: CommissionTypeLiteral
    # 0 is allowed and means "no platform fee".
    commission_value: Decimal = Field(ge=0, decimal_places=2)
    platform_fee_refund_behavior: FeeRefundBehaviorLiteral
    live_payments_enabled: bool
    subscription_grace_days: int = Field(default=3, ge=0, le=90)
    # Stripe Tax at checkout. Stripe Tax must be activated on the Stripe account first.
    automatic_tax_enabled: bool = False

    @model_validator(mode="after")
    def _percent_ceiling(self) -> PaymentSettingsUpdate:
        if self.commission_type == "percent" and self.commission_value > 100:
            raise ValueError(
                "commission_value must be between 0 and 100 when commission_type is percent"
            )
        return self


class PaymentSettingsRead(BaseModel):
    id: uuid.UUID
    seeker_reschedule_window_hours: int
    advisor_cancellation_window_hours: int
    commission_type: CommissionTypeLiteral
    commission_value: Decimal
    platform_fee_refund_behavior: FeeRefundBehaviorLiteral
    live_payments_enabled: bool
    subscription_grace_days: int = 3
    automatic_tax_enabled: bool = False
    updated_at: datetime | None = None
    updated_by: uuid.UUID | None = None


class PaymentSettingChangeRead(BaseModel):
    id: uuid.UUID
    setting_key: str
    old_value: str | None
    new_value: str | None
    changed_by: uuid.UUID | None
    changed_by_name: str | None = None
    changed_at: datetime


# ── Stripe credentials (PAY-107 / PAY-108) ───────────────────────────────────


class StripeKeysUpdate(BaseModel):
    """What the admin typed into the Stripe Configuration form.

    Every field is optional so the form can be saved without retyping a secret the
    admin is not changing. ``None`` means "leave it as it is"; clearing a value is a
    separate, deliberate action rather than an empty text box.
    """

    secret_key: str | None = Field(default=None, max_length=255)
    publishable_key: str | None = Field(default=None, max_length=255)
    webhook_secret: str | None = Field(default=None, max_length=255)
    # The admin's declaration of which Stripe environment this platform is on. Every
    # supplied key is checked against it, so a live key cannot arrive under "Test".
    mode: StripeModeLiteral | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> StripeKeysUpdate:
        if not any((self.secret_key, self.publishable_key, self.webhook_secret, self.mode)):
            raise ValueError("Provide at least one value to save")
        return self


class StripeKeyStatus(BaseModel):
    """What the form is allowed to show back.

    Never the secret key or the signing secret — only whether each is set, the last
    four characters so an admin can tell which key is loaded, and where it came from
    (PAY-108 AC 2).
    """

    source: Literal["database", "environment", "none"]
    mode: Literal["test", "live", "unknown"]
    mode_is_chosen: bool = False
    secret_key_set: bool
    secret_key_last4: str | None = None
    publishable_key: str | None = None
    webhook_secret_set: bool
    webhook_secret_last4: str | None = None
