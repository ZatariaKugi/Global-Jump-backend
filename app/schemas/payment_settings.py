"""Admin payment settings schemas (EPIC 04, PAY-107)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CommissionTypeLiteral = Literal["percent", "fixed"]
FeeRefundBehaviorLiteral = Literal["retained", "refunded"]

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
