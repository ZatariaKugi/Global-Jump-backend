"""Dynamic payment configuration (EPIC 04, PAY-109).

Single place every payment, refund, reschedule and cancellation rule is read from.
Values live in the ``platform_payment_settings`` row and are read at execution
time; the ``.env`` commission only seeds the row on the very first read, so the
admin can change any rule without a redeploy.

``PaymentConfig`` is a frozen snapshot; callers never touch the ORM row. A short
in-process cache keeps hot paths (booking list fee split, checkout) from hitting
the table on every request; ``update()`` invalidates it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.platform_payment_settings import (
    CommissionType,
    FeeRefundBehavior,
    PlatformPaymentSettings,
    PlatformSettingChange,
)
from app.models.user import User
from app.schemas.payment_settings import (
    PaymentSettingChangeRead,
    PaymentSettingsRead,
    PaymentSettingsUpdate,
)

CACHE_TTL_SECONDS = 30.0
_CENT = Decimal("0.01")

# Keys that are audited, in the order they are compared and reported.
SETTING_KEYS: tuple[str, ...] = (
    "seeker_reschedule_window_hours",
    "advisor_cancellation_window_hours",
    "commission_type",
    "commission_value",
    "platform_fee_refund_behavior",
    "live_payments_enabled",
    "subscription_grace_days",
)


@dataclass(frozen=True, slots=True)
class PaymentConfig:
    seeker_reschedule_window_hours: int
    advisor_cancellation_window_hours: int
    commission_type: str  # "percent" | "fixed"
    commission_value: Decimal
    platform_fee_refund_behavior: str  # "retained" | "refunded"
    live_payments_enabled: bool
    subscription_grace_days: int = 3


_cache: tuple[PaymentConfig, float] | None = None


def invalidate_cache() -> None:
    global _cache
    _cache = None


def _from_row(row: PlatformPaymentSettings) -> PaymentConfig:
    return PaymentConfig(
        seeker_reschedule_window_hours=int(row.seeker_reschedule_window_hours),
        advisor_cancellation_window_hours=int(row.advisor_cancellation_window_hours),
        commission_type=CommissionType(row.commission_type).value,
        commission_value=Decimal(str(row.commission_value)),
        platform_fee_refund_behavior=FeeRefundBehavior(row.platform_fee_refund_behavior).value,
        live_payments_enabled=bool(row.live_payments_enabled),
        subscription_grace_days=int(row.subscription_grace_days),
    )


def _seed_row() -> PlatformPaymentSettings:
    """Defaults for the very first row: env commission rate, document windows."""
    rate = Decimal(str(get_settings().PLATFORM_COMMISSION_RATE))
    percent = (rate * 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    return PlatformPaymentSettings(
        seeker_reschedule_window_hours=3,
        advisor_cancellation_window_hours=2,
        commission_type=CommissionType.percent,
        commission_value=percent,
        platform_fee_refund_behavior=FeeRefundBehavior.retained,
        live_payments_enabled=False,
    )


async def _get_row(session: AsyncSession) -> PlatformPaymentSettings:
    rows = list((await session.execute(select(PlatformPaymentSettings))).scalars().all())
    if rows:
        if len(rows) > 1:  # singleton drift: keep the oldest, drop the rest
            for extra in rows[1:]:
                await session.delete(extra)
            await session.flush()
        return rows[0]
    row = _seed_row()
    session.add(row)
    await session.flush()
    return row


async def get_config(session: AsyncSession) -> PaymentConfig:
    """The current rules; seeds the settings row on first use."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[1] < CACHE_TTL_SECONDS:
        return _cache[0]
    config = _from_row(await _get_row(session))
    _cache = (config, now)
    return config


def compute_platform_fee(config: PaymentConfig, price_usd: Decimal | float) -> Decimal:
    """Platform fee for one consultation price, in USD, rounded to cents.

    Percent: ``price * value / 100``. Fixed: ``value``. Never negative and never
    above the price itself (a fixed fee larger than a cheap consultation is capped).
    """
    price = Decimal(str(price_usd))
    if price <= 0:
        return Decimal("0.00")
    if config.commission_type == CommissionType.fixed.value:
        fee = Decimal(str(config.commission_value))
    else:
        fee = price * Decimal(str(config.commission_value)) / Decimal(100)
    fee = max(Decimal(0), min(fee, price))
    return fee.quantize(_CENT, rounding=ROUND_HALF_UP)


def _as_audit_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if hasattr(value, "value"):  # StrEnum
        return str(value.value)
    return str(value)


async def update(
    session: AsyncSession, data: PaymentSettingsUpdate, admin_id: uuid.UUID
) -> PaymentConfig:
    """Apply an admin save. Writes one audit row per key whose value changed."""
    row = await _get_row(session)
    before = _from_row(row)
    changes: list[PlatformSettingChange] = []
    for key in SETTING_KEYS:
        old = getattr(before, key)
        new = getattr(data, key)
        old_s, new_s = _as_audit_value(old), _as_audit_value(new)
        if old_s == new_s:
            continue
        setattr(row, key, new)
        changes.append(
            PlatformSettingChange(
                settings_id=row.id,
                setting_key=key,
                old_value=old_s,
                new_value=new_s,
                changed_by=admin_id,
            )
        )
    if changes:
        row.updated_by = admin_id
        session.add(row)
        session.add_all(changes)
        await session.flush()
    invalidate_cache()
    return _from_row(row)


async def get_read(session: AsyncSession) -> PaymentSettingsRead:
    row = await _get_row(session)
    return PaymentSettingsRead(
        id=row.id,
        seeker_reschedule_window_hours=row.seeker_reschedule_window_hours,
        advisor_cancellation_window_hours=row.advisor_cancellation_window_hours,
        commission_type=CommissionType(row.commission_type).value,
        commission_value=Decimal(str(row.commission_value)),
        platform_fee_refund_behavior=FeeRefundBehavior(row.platform_fee_refund_behavior).value,
        live_payments_enabled=row.live_payments_enabled,
        subscription_grace_days=row.subscription_grace_days,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def audit_stmt() -> Select[tuple[PlatformSettingChange]]:
    return select(PlatformSettingChange).order_by(PlatformSettingChange.changed_at.desc())


async def build_audit_reads(
    session: AsyncSession, changes: list[PlatformSettingChange]
) -> list[PaymentSettingChangeRead]:
    admin_ids = {c.changed_by for c in changes if c.changed_by is not None}
    names: dict[uuid.UUID, str | None] = {}
    if admin_ids:
        rows = (
            await session.execute(
                select(User.id, User.full_name, User.email).where(User.id.in_(admin_ids))
            )
        ).all()
        names = {user_id: (full_name or email) for user_id, full_name, email in rows}
    return [
        PaymentSettingChangeRead(
            id=c.id,
            setting_key=c.setting_key,
            old_value=c.old_value,
            new_value=c.new_value,
            changed_by=c.changed_by,
            changed_by_name=names.get(c.changed_by) if c.changed_by else None,
            changed_at=c.changed_at,
        )
        for c in changes
    ]
