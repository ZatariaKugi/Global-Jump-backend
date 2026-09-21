
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.models.advisor_profile import AdvisorOfferedService, AdvisorProfile
from app.models.booking import Booking, BookingStatus
from app.schemas.advisor_profile import (
    AdminOfferedServiceRead,
    OfferedServiceCreateRequest,
    OfferedServiceItemInput,
    OfferedServicePublicRead,
    OfferedServiceRead,
    OfferedServicesReplaceRequest,
    OfferedServiceUpdateRequest,
)

DEFAULT_DURATION_MINUTES = 30


def _catalog_in_use_message(count: int, action: str) -> str:
    advisor_label = "advisor" if count == 1 else "advisors"
    return f"It's in use by {count} {advisor_label} you can not {action}"


def _service_id(row: AdvisorOfferedService) -> uuid.UUID:
    return row.service_id or row.id


def _to_public(row: AdvisorOfferedService) -> OfferedServicePublicRead:
    return OfferedServicePublicRead(
        id=row.id,
        service_id=_service_id(row),
        name=row.name,
        price_usd=row.price_usd,
        duration_minutes=row.duration_minutes or DEFAULT_DURATION_MINUTES,
    )


def _to_read(
    row: AdvisorOfferedService,
    *,
    has_active_bookings: bool = False,
) -> OfferedServiceRead:
    return OfferedServiceRead(
        id=row.id,
        service_id=_service_id(row),
        name=row.name,
        price_usd=row.price_usd,
        duration_minutes=row.duration_minutes or DEFAULT_DURATION_MINUTES,
        has_active_bookings=has_active_bookings,
    )


def _to_admin_read(row: AdvisorOfferedService) -> AdminOfferedServiceRead:
    return AdminOfferedServiceRead(id=row.id, service_id=_service_id(row), name=row.name)


async def get_profile_by_user_id(session: AsyncSession, user_id: uuid.UUID) -> AdvisorProfile:
    profile = await session.scalar(select(AdvisorProfile).where(AdvisorProfile.user_id == user_id))
    if profile is None:
        raise NotFoundError("Advisor profile not found")
    return profile


async def _catalog_rows(
    session: AsyncSession,
    *,
    service_id: uuid.UUID | None = None,
) -> list[AdvisorOfferedService]:
    stmt = (
        select(AdvisorOfferedService)
        .where(AdvisorOfferedService.profile_id.is_(None))
        .order_by(AdvisorOfferedService.name.asc())
    )
    if service_id is not None:
        stmt = stmt.where(AdvisorOfferedService.id == service_id)
    return list((await session.execute(stmt)).scalars().all())


async def list_all_public(
    session: AsyncSession,
    *,
    service_id: uuid.UUID | None = None,
) -> list[OfferedServicePublicRead]:
    return [_to_public(row) for row in await _catalog_rows(session, service_id=service_id)]


async def list_all_admin(
    session: AsyncSession,
    *,
    service_id: uuid.UUID | None = None,
) -> list[AdminOfferedServiceRead]:
    return [_to_admin_read(row) for row in await _catalog_rows(session, service_id=service_id)]


async def list_catalog_for_advisor(
    session: AsyncSession,
    *,
    service_id: uuid.UUID | None = None,
) -> list[OfferedServiceRead]:
    return [_to_read(row) for row in await _catalog_rows(session, service_id=service_id)]


async def list_for_advisor_public(
    session: AsyncSession, advisor_id: uuid.UUID
) -> list[OfferedServicePublicRead]:
    profile = await get_profile_by_user_id(session, advisor_id)
    return [_to_public(row) for row in (profile.offered_services or [])]


async def list_for_advisor(
    session: AsyncSession, advisor_id: uuid.UUID
) -> list[OfferedServiceRead]:
    profile = await get_profile_by_user_id(session, advisor_id)
    services = list(profile.offered_services or [])
    booked_service_ids = await _active_booked_service_ids(
        session,
        advisor_id,
        {_service_id(service) for service in services},
    )
    return [
        _to_read(service, has_active_bookings=_service_id(service) in booked_service_ids)
        for service in services
    ]


async def _active_booked_service_ids(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    service_ids: set[uuid.UUID],
) -> set[uuid.UUID]:
    if not service_ids:
        return set()
    result = await session.execute(
        select(Booking.service_id)
        .where(Booking.advisor_id == advisor_id)
        .where(Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]))
        .where(Booking.service_id.in_(service_ids))
        .distinct()
    )
    return {service_id for service_id in result.scalars() if service_id is not None}


async def _catalog_row(
    session: AsyncSession,
    service_id: uuid.UUID,
) -> AdvisorOfferedService:
    row = await session.scalar(
        select(AdvisorOfferedService).where(
            AdvisorOfferedService.id == service_id,
            AdvisorOfferedService.profile_id.is_(None),
        )
    )
    if row is None:
        raise AppError("Service is not available", code="unknown_service")
    return row


def _build_row(
    profile_id: uuid.UUID,
    item: OfferedServiceItemInput | OfferedServiceCreateRequest,
    catalog: AdvisorOfferedService,
) -> AdvisorOfferedService:
    return AdvisorOfferedService(
        profile_id=profile_id,
        service_id=catalog.id,
        name=catalog.name,
        price_usd=item.price_usd,
        duration_minutes=item.duration_minutes,
    )


async def create(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    data: OfferedServiceCreateRequest,
    *,
    actor_id: uuid.UUID | None = None,
) -> OfferedServiceRead:
    if data.service_id is None:
        raise AppError("service_id is required", code="service_id_required")
    profile = await get_profile_by_user_id(session, advisor_id)
    catalog = await _catalog_row(session, data.service_id)
    if any(_service_id(row) == catalog.id for row in (profile.offered_services or [])):
        raise ConflictError("Advisor already offers this service", code="duplicate_service")
    row = _build_row(profile.id, data, catalog)
    session.add(row)
    profile.updated_by = actor_id or advisor_id
    session.add(profile)
    await session.flush()
    await session.refresh(row)
    return _to_read(row)


async def create_global(
    session: AsyncSession,
    data: OfferedServiceCreateRequest,
) -> OfferedServiceRead:
    if not data.name:
        raise AppError("name is required", code="name_required")
    if await _catalog_name_exists(session, data.name):
        raise ConflictError(f"Global service {data.name!r} already exists")
    row = AdvisorOfferedService(
        profile_id=None,
        name=data.name,
        price_usd=None,
        duration_minutes=DEFAULT_DURATION_MINUTES,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return _to_read(row)


async def _catalog_name_exists(
    session: AsyncSession,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    stmt = select(AdvisorOfferedService.id).where(
        AdvisorOfferedService.profile_id.is_(None),
        func.lower(AdvisorOfferedService.name) == name.casefold(),
    )
    if exclude_id is not None:
        stmt = stmt.where(AdvisorOfferedService.id != exclude_id)
    return await session.scalar(stmt) is not None


async def count_catalog_usage(session: AsyncSession, service_id: uuid.UUID) -> int:
    await _catalog_row(session, service_id)
    return (
        await session.scalar(
            select(func.count())
            .select_from(AdvisorOfferedService)
            .where(AdvisorOfferedService.profile_id.is_not(None))
            .where(AdvisorOfferedService.service_id == service_id)
        )
        or 0
    )


async def get_row(session: AsyncSession, offered_service_id: uuid.UUID) -> AdvisorOfferedService:
    row = await session.get(AdvisorOfferedService, offered_service_id)
    if row is None:
        raise NotFoundError("Offered service not found")
    return row


async def _profile_for_row(session: AsyncSession, row: AdvisorOfferedService) -> AdvisorProfile:
    profile = await session.get(AdvisorProfile, row.profile_id)
    if profile is None:
        raise NotFoundError("Advisor profile not found")
    return profile


async def update(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    offered_service_id: uuid.UUID,
    data: OfferedServiceUpdateRequest,
    *,
    actor_id: uuid.UUID | None = None,
) -> OfferedServiceRead:
    profile = await get_profile_by_user_id(session, advisor_id)
    target = next(
        (row for row in (profile.offered_services or []) if row.id == offered_service_id),
        None,
    )
    if target is None:
        raise NotFoundError("Offered service not found")
    return await _apply_update(session, profile, target, data, actor_id=actor_id or advisor_id)


async def update_by_id(
    session: AsyncSession,
    offered_service_id: uuid.UUID,
    data: OfferedServiceUpdateRequest,
    *,
    actor_id: uuid.UUID | None = None,
) -> OfferedServiceRead:
    target = await get_row(session, offered_service_id)
    if target.profile_id is None:
        in_use = await count_catalog_usage(session, target.id)
        if in_use:
            raise ConflictError(
                _catalog_in_use_message(in_use, "edit"),
                code="catalog_service_in_use",
            )
        new_name = data.name
        if new_name is not None and await _catalog_name_exists(
            session, new_name, exclude_id=target.id
        ):
            raise ConflictError(f"Global service {new_name!r} already exists")
    profile = await _profile_for_row(session, target) if target.profile_id else None
    return await _apply_update(session, profile, target, data, actor_id=actor_id)


async def _apply_update(
    session: AsyncSession,
    profile: AdvisorProfile | None,
    target: AdvisorOfferedService,
    data: OfferedServiceUpdateRequest,
    *,
    actor_id: uuid.UUID | None,
) -> OfferedServiceRead:
    fields = data.model_dump(exclude_unset=True)
    for field, value in fields.items():
        if value is not None:
            setattr(target, field, value)

    if profile is None and "name" in fields and target.name:
        siblings = (
            await session.scalars(
                select(AdvisorOfferedService).where(
                    AdvisorOfferedService.profile_id.is_not(None),
                    AdvisorOfferedService.service_id == target.id,
                )
            )
        ).all()
        for sibling in siblings:
            sibling.name = target.name

    if profile is not None:
        profile.updated_by = actor_id
        session.add(profile)
    await session.flush()
    await session.refresh(target)
    return _to_read(target)


async def replace_for_advisor(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    data: OfferedServicesReplaceRequest,
    *,
    actor_id: uuid.UUID | None = None,
) -> list[OfferedServiceRead]:
    profile = await get_profile_by_user_id(session, advisor_id)
    catalog = await _catalog_rows(session)
    by_id = {row.id: row for row in catalog}

    rows: list[tuple[OfferedServiceItemInput, AdvisorOfferedService]] = []
    seen: set[uuid.UUID] = set()
    for item in data.services:
        catalog_row = by_id.get(item.service_id)
        if catalog_row is None:
            raise AppError("Service is not available", code="unknown_service")
        if item.service_id in seen:
            raise AppError("Duplicate service_id in request", code="duplicate_service_id")
        seen.add(item.service_id)
        rows.append((item, catalog_row))

    existing = {_service_id(row): row for row in (profile.offered_services or [])}
    kept: list[AdvisorOfferedService] = []
    for item, catalog_row in rows:
        row = existing.pop(item.service_id, None)
        if row is None:
            row = _build_row(profile.id, item, catalog_row)
            profile.offered_services.append(row)
        else:
            row.service_id = catalog_row.id
            row.name = catalog_row.name
            row.price_usd = item.price_usd
            row.duration_minutes = item.duration_minutes
        kept.append(row)

    removed = list(existing.values())
    removed_ids = {_service_id(row) for row in removed}
    booked_ids = await _active_booked_service_ids(session, advisor_id, removed_ids)
    if booked_ids:
        raise ConflictError(
            "You have booking for this service",
            code="service_has_active_bookings",
            detail={"service_ids": [str(value) for value in sorted(booked_ids, key=str)]},
        )
    for stale in removed:
        profile.offered_services.remove(stale)

    profile.updated_by = actor_id or advisor_id
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    booked_ids = await _active_booked_service_ids(
        session, advisor_id, {_service_id(row) for row in kept}
    )
    return [
        _to_read(row, has_active_bookings=_service_id(row) in booked_ids) for row in kept
    ]


async def delete(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    offered_service_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
) -> None:
    profile = await get_profile_by_user_id(session, advisor_id)
    target = next(
        (row for row in (profile.offered_services or []) if row.id == offered_service_id),
        None,
    )
    if target is None:
        raise NotFoundError("Offered service not found")
    if await _active_booked_service_ids(session, advisor_id, {_service_id(target)}):
        raise ConflictError("You have booking for this service", code="service_has_active_bookings")
    await session.delete(target)
    profile.updated_by = actor_id or advisor_id
    session.add(profile)
    await session.flush()


async def delete_by_id(
    session: AsyncSession,
    offered_service_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
) -> None:
    target = await get_row(session, offered_service_id)
    if target.profile_id is None:
        in_use = await count_catalog_usage(session, target.id)
        if in_use:
            raise ConflictError(
                _catalog_in_use_message(in_use, "delete"),
                code="catalog_service_in_use",
            )
    else:
        profile = await _profile_for_row(session, target)
        if await _active_booked_service_ids(session, profile.user_id, {_service_id(target)}):
            raise ConflictError(
                "You have booking for this service",
                code="service_has_active_bookings",
            )
    await session.delete(target)
    await session.flush()
