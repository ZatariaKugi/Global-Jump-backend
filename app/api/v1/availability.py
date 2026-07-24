"""Advisor availability endpoints (PRD §3.6).

Registered *before* the advisors router so ``/advisors/me/availability`` wins
over the ``/advisors/{advisor_id}`` path.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    CurrentPrincipal,
    Principal,
    RequestIdDep,
    require_verified_advisor,
)
from app.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from app.db.session import SessionDep
from app.models.advisor_availability import (
    AdvisorAvailabilityOverride,
    AdvisorWeeklySlot,
)
from app.models.booking import Booking
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.availability import (
    FreeSlotRead,
    OverrideInput,
    OverrideRead,
    WeeklySlotRead,
    WeeklySlotsUpdate,
)
from app.schemas.response import Meta, ResponseEnvelope
from app.services import availability_service

router = APIRouter(prefix="/advisors", tags=["availability"])

VerifiedAdvisorDep = Annotated[Principal, Depends(require_verified_advisor)]

MAX_RANGE_DAYS = 60
# Default increment when ``duration_minutes`` is omitted. Pass the booked service
# duration when booking/rescheduling a specific service.
DEFAULT_SLOT_MINUTES = 30


def _slot_read(slot: AdvisorWeeklySlot) -> WeeklySlotRead:
    return WeeklySlotRead(
        id=slot.id,
        weekday=slot.weekday,
        start_time=slot.start_time,
        end_time=slot.end_time,
        timezone=slot.timezone,
    )


def _override_read(override: AdvisorAvailabilityOverride) -> OverrideRead:
    all_day = override.start_time is None or override.end_time is None
    return OverrideRead(
        id=override.id,
        date=override.date,
        is_available=override.is_available,
        reason=override.reason,
        all_day=all_day,
        start_time=override.start_time,
        end_time=override.end_time,
        timezone=override.timezone,
    )


@router.get("/me/availability", response_model=ResponseEnvelope[list[WeeklySlotRead]])
async def get_my_availability(
    principal: VerifiedAdvisorDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[WeeklySlotRead]]:
    slots = await availability_service.list_weekly_slots(session, principal.id)
    return ResponseEnvelope[list[WeeklySlotRead]](
        data=[_slot_read(s) for s in slots],
        meta=Meta(request_id=request_id),
    )


@router.put("/me/availability", response_model=ResponseEnvelope[list[WeeklySlotRead]])
async def replace_my_availability(
    data: WeeklySlotsUpdate,
    principal: VerifiedAdvisorDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[WeeklySlotRead]]:
    slots = await availability_service.set_weekly_slots(session, principal.id, data.slots)
    return ResponseEnvelope[list[WeeklySlotRead]](
        data=[_slot_read(s) for s in slots],
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/me/availability/overrides",
    response_model=ResponseEnvelope[list[OverrideRead]],
)
async def list_my_overrides(
    principal: VerifiedAdvisorDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[OverrideRead]]:
    overrides = await availability_service.list_overrides(session, principal.id)
    return ResponseEnvelope[list[OverrideRead]](
        data=[_override_read(o) for o in overrides],
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/me/availability/overrides",
    status_code=201,
    response_model=ResponseEnvelope[OverrideRead],
)
async def add_my_override(
    data: OverrideInput,
    principal: VerifiedAdvisorDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[OverrideRead]:
    override = await availability_service.add_override(session, principal.id, data)
    return ResponseEnvelope[OverrideRead](
        data=_override_read(override),
        meta=Meta(request_id=request_id),
    )


@router.delete("/me/availability/overrides/{override_id}", status_code=204)
async def delete_my_override(
    override_id: uuid.UUID,
    principal: VerifiedAdvisorDep,
    session: SessionDep,
) -> None:
    override = await availability_service.get_override(session, principal.id, override_id)
    if override is None:
        raise NotFoundError("Override not found")
    await availability_service.delete_override(session, override)


@router.get(
    "/{advisor_id}/availability",
    response_model=ResponseEnvelope[list[FreeSlotRead]],
    summary="List free bookable slots",
    description=(
        "Returns UTC increments of ``duration_minutes`` (default **30**, range 15–480) "
        "within the date range. Pass the booking/service duration when scheduling a "
        "specific service. Use ``exclude_booking_id`` when rescheduling so the current "
        "booking's slot remains selectable."
    ),
)
async def get_advisor_free_slots(
    advisor_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
    date_from: Annotated[date, Query()],
    date_to: Annotated[date, Query()],
    duration_minutes: Annotated[
        int,
        Query(
            ge=15,
            le=480,
            description="Slot length in minutes. Default 30 when omitted.",
        ),
    ] = DEFAULT_SLOT_MINUTES,
    exclude_booking_id: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Omit this booking from the busy set (reschedule picker). "
                "Caller must be the seeker or advisor on that booking."
            ),
        ),
    ] = None,
) -> ResponseEnvelope[list[FreeSlotRead]]:
    if date_to < date_from:
        raise AppError("date_to must be on or after date_from", code="invalid_range")
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise AppError(f"Range may not exceed {MAX_RANGE_DAYS} days", code="invalid_range")

    advisor = await session.get(User, advisor_id)
    if (
        advisor is None
        or advisor.role != UserRole.advisor
        or not advisor.is_active
        or advisor.verification_status != VerificationStatus.approved
    ):
        raise NotFoundError("Advisor not found")

    if exclude_booking_id is not None:
        booking = await session.get(Booking, exclude_booking_id)
        if (
            booking is None
            or booking.advisor_id != advisor_id
            or principal.id not in (booking.seeker_id, booking.advisor_id)
        ):
            raise PermissionDeniedError("Cannot exclude this booking")

    slots = await availability_service.free_slots(
        session,
        advisor_id,
        date_from,
        date_to,
        duration_minutes,
        exclude_booking_id=exclude_booking_id,
    )
    return ResponseEnvelope[list[FreeSlotRead]](
        data=[FreeSlotRead(start_utc=s, end_utc=e) for s, e in slots],
        meta=Meta(request_id=request_id),
    )
