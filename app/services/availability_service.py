"""Advisor availability — weekly slot management and free-slot computation.

Weekly slots are stored as advisor-local wall times with an IANA timezone;
``free_slots`` expands them to concrete UTC intervals per date (DST-correct via
``zoneinfo``), removes blocked override dates, subtracts active bookings, and
chops the remainder into bookable increments.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.advisor_availability import (
    AdvisorAvailabilityOverride,
    AdvisorWeeklySlot,
)
from app.models.booking import Booking, BookingStatus
from app.schemas.availability import OverrideInput, WeeklySlotInput

ACTIVE_BOOKING_STATUSES = (BookingStatus.pending, BookingStatus.confirmed)


async def list_weekly_slots(
    session: AsyncSession, advisor_id: uuid.UUID
) -> list[AdvisorWeeklySlot]:
    result = await session.execute(
        select(AdvisorWeeklySlot)
        .where(AdvisorWeeklySlot.advisor_id == advisor_id)
        .order_by(AdvisorWeeklySlot.weekday, AdvisorWeeklySlot.start_time)
    )
    return list(result.scalars().all())


async def set_weekly_slots(
    session: AsyncSession, advisor_id: uuid.UUID, slots: list[WeeklySlotInput]
) -> list[AdvisorWeeklySlot]:
    """Replace the advisor's whole weekly schedule."""
    await session.execute(
        delete(AdvisorWeeklySlot).where(AdvisorWeeklySlot.advisor_id == advisor_id)
    )
    rows = [
        AdvisorWeeklySlot(
            advisor_id=advisor_id,
            weekday=slot.weekday,
            start_time=slot.start_time,
            end_time=slot.end_time,
            timezone=slot.timezone,
        )
        for slot in slots
    ]
    session.add_all(rows)
    await session.flush()
    return await list_weekly_slots(session, advisor_id)


async def list_overrides(
    session: AsyncSession, advisor_id: uuid.UUID
) -> list[AdvisorAvailabilityOverride]:
    result = await session.execute(
        select(AdvisorAvailabilityOverride)
        .where(AdvisorAvailabilityOverride.advisor_id == advisor_id)
        .order_by(AdvisorAvailabilityOverride.date)
    )
    return list(result.scalars().all())


async def add_override(
    session: AsyncSession, advisor_id: uuid.UUID, data: OverrideInput
) -> AdvisorAvailabilityOverride:
    override = AdvisorAvailabilityOverride(
        advisor_id=advisor_id,
        date=data.date,
        is_available=False,
        reason=data.reason,
    )
    session.add(override)
    await session.flush()
    await session.refresh(override)
    return override


async def get_override(
    session: AsyncSession, advisor_id: uuid.UUID, override_id: uuid.UUID
) -> AdvisorAvailabilityOverride | None:
    override = await session.get(AdvisorAvailabilityOverride, override_id)
    if override is None or override.advisor_id != advisor_id:
        return None
    return override


async def delete_override(session: AsyncSession, override: AdvisorAvailabilityOverride) -> None:
    await session.delete(override)
    await session.flush()


def _expand_slot_for_date(slot: AdvisorWeeklySlot, day: date) -> tuple[datetime, datetime] | None:
    """Convert a weekly slot to a concrete UTC interval on ``day`` (advisor-local)."""
    if day.weekday() != slot.weekday:
        return None
    tz = ZoneInfo(slot.timezone)
    start_local = datetime.combine(day, slot.start_time, tzinfo=tz)
    end_local = datetime.combine(day, slot.end_time, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


async def free_slots(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    date_from: date,
    date_to: date,
    duration_minutes: int,
) -> list[tuple[datetime, datetime]]:
    """Bookable UTC increments of ``duration_minutes`` between the two dates."""
    weekly = await list_weekly_slots(session, advisor_id)
    if not weekly:
        return []

    overrides = await list_overrides(session, advisor_id)
    blocked_days = {o.date for o in overrides if not o.is_available}

    bookings_result = await session.execute(
        select(Booking)
        .where(Booking.advisor_id == advisor_id)
        .where(Booking.status.in_(ACTIVE_BOOKING_STATUSES))
        .where(Booking.scheduled_end >= datetime.combine(date_from, datetime.min.time(), UTC))
    )
    busy = [
        (as_utc(b.scheduled_start), as_utc(b.scheduled_end))
        for b in bookings_result.scalars().all()
    ]

    step = timedelta(minutes=duration_minutes)
    slots: list[tuple[datetime, datetime]] = []
    day = date_from
    while day <= date_to:
        if day not in blocked_days:
            for weekly_slot in weekly:
                interval = _expand_slot_for_date(weekly_slot, day)
                if interval is None:
                    continue
                cursor, window_end = interval
                while cursor + step <= window_end:
                    candidate = (cursor, cursor + step)
                    if not any(s < candidate[1] and candidate[0] < e for s, e in busy):
                        slots.append(candidate)
                    cursor += step
        day += timedelta(days=1)

    slots.sort(key=lambda pair: pair[0])
    return slots


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat stored values as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
