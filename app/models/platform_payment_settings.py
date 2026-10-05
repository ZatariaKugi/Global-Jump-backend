"""Admin-managed payment rules (EPIC 04, PAY-107 / PAY-109).

One ``platform_payment_settings`` row holds every business rule the payment flows
read at execution time: the seeker reschedule window, the advisor cancellation
window, the platform commission (percent or fixed USD), whether the platform fee is
refunded when an advisor cancels, and the live-payments switch. Nothing here is
read from ``.env`` after the first seed — see ``payment_config_service``.

Tax withholding is deliberately absent (client decision 2026-09-23).

``platform_setting_changes`` is the audit trail: one row per changed key per save.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.base_model import BaseModel


class CommissionType(StrEnum):
    percent = "percent"  # commission_value is 0..100
    fixed = "fixed"  # commission_value is USD, >= 0


class FeeRefundBehavior(StrEnum):
    retained = "retained"  # platform keeps its fee when the advisor cancels (document default)
    refunded = "refunded"  # platform fee is returned to the seeker as well


class PlatformPaymentSettings(BaseModel):
    """Singleton row. ``payment_config_service.get_config`` seeds it on first read."""

    __tablename__ = "platform_payment_settings"

    seeker_reschedule_window_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    advisor_cancellation_window_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    commission_type: Mapped[CommissionType] = mapped_column(
        SAEnum(CommissionType, name="commission_type"),
        nullable=False,
        default=CommissionType.percent,
        server_default=CommissionType.percent.value,
    )
    # Percent (0..100) or USD depending on ``commission_type``. 0 switches the fee off.
    commission_value: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("15"), server_default="15"
    )
    platform_fee_refund_behavior: Mapped[FeeRefundBehavior] = mapped_column(
        SAEnum(FeeRefundBehavior, name="fee_refund_behavior"),
        nullable=False,
        default=FeeRefundBehavior.retained,
        server_default=FeeRefundBehavior.retained.value,
    )
    live_payments_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Days of access kept after a failed renewal (decision D8).
    subscription_grace_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    # Stripe credentials, admin-configurable (PAY-107) and never in source control
    # (PAY-108). The two secrets are AES-256-GCM ciphertext from ``encryption.py``;
    # the publishable key is public by definition and is stored as it is. All three
    # are null until an admin saves them, and the environment is used until then.
    stripe_secret_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_webhook_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_publishable_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # "test" or "live", chosen by the admin rather than inferred from a key prefix.
    # Declaring it lets the save refuse keys that contradict it — a live key pasted
    # while Test is selected is the one mistake here that costs real money.
    stripe_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)


class PlatformSettingChange(Base):
    """Append-only audit: who changed which setting, from what, to what, when."""

    __tablename__ = "platform_setting_changes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    settings_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_payment_settings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    setting_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    old_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
