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
from app.core.exceptions import AppError, NotFoundError
from app.db.session import SessionDep
from app.models.advisor_availability import (
    AdvisorAvailabilityOverride,
    AdvisorWeeklySlot,
)
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
    return OverrideRead(
        id=override.id,
        date=override.date,
        is_available=override.is_available,
        reason=override.reason,
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
)
async def get_advisor_free_slots(
    advisor_id: uuid.UUID,
    _principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
    date_from: Annotated[date, Query()],
    date_to: Annotated[date, Query()],
    duration_minutes: Annotated[int, Query(ge=15, le=480)] = DEFAULT_SLOT_MINUTES,
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

    slots = await availability_service.free_slots(
        session, advisor_id, date_from, date_to, duration_minutes
    )
    return ResponseEnvelope[list[FreeSlotRead]](
        data=[FreeSlotRead(start_utc=s, end_utc=e) for s, e in slots],
        meta=Meta(request_id=request_id),
    )
