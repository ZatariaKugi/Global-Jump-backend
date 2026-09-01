"""CRUD for ``advisor_offered_services`` (category tags + optional price/duration)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.models.advisor_profile import AdvisorOfferedService, AdvisorProfile
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


def _to_public(row: AdvisorOfferedService) -> OfferedServicePublicRead:
    return OfferedServicePublicRead(
        id=row.id,
        service_type=row.service_type,
    )


def _to_read(row: AdvisorOfferedService) -> OfferedServiceRead:
    return OfferedServiceRead(
        id=row.id,
        service_type=row.service_type,
        price_usd=row.price_usd,
        duration_minutes=row.duration_minutes or DEFAULT_DURATION_MINUTES,
    )


def _to_admin_read(row: AdvisorOfferedService) -> AdminOfferedServiceRead:
    return AdminOfferedServiceRead(
        id=row.id,
        service_type=row.service_type,
    )


async def get_profile_by_user_id(
    session: AsyncSession, user_id: uuid.UUID
) -> AdvisorProfile:
    profile = await session.scalar(
        select(AdvisorProfile).where(AdvisorProfile.user_id == user_id)
    )
    if profile is None:
        raise NotFoundError("Advisor profile not found")
    return profile


async def list_all_public(
    session: AsyncSession,
    *,
    service_type: str | None = None,
) -> list[OfferedServicePublicRead]:
    """Seeker/public: admin catalog (global rows) — id + service_type only."""
    return [
        OfferedServicePublicRead(id=row.id, service_type=row.service_type)
        for row in await _catalog_rows(session, service_type=service_type)
    ]


async def list_all_admin(
    session: AsyncSession,
    *,
    service_type: str | None = None,
) -> list[AdminOfferedServiceRead]:
    """Admin catalog: global rows (``profile_id`` IS NULL) — id + service_type only."""
    return [_to_admin_read(row) for row in await _catalog_rows(session, service_type=service_type)]


async def list_catalog_for_advisor(
    session: AsyncSession,
    *,
    service_type: str | None = None,
) -> list[OfferedServiceRead]:
    """Advisor-facing catalog — same global rows admin manages."""
    return [_to_read(row) for row in await _catalog_rows(session, service_type=service_type)]


async def _catalog_rows(
    session: AsyncSession,
    *,
    service_type: str | None = None,
) -> list[AdvisorOfferedService]:
    """Admin-managed catalog entries (not tied to a specific advisor profile)."""
    stmt = (
        select(AdvisorOfferedService)
        .where(AdvisorOfferedService.profile_id.is_(None))
        .order_by(AdvisorOfferedService.service_type.asc())
    )
    if service_type is not None:
        stmt = stmt.where(AdvisorOfferedService.service_type == service_type)
    return list((await session.execute(stmt)).scalars().all())


async def list_for_advisor_public(
    session: AsyncSession, advisor_id: uuid.UUID
) -> list[OfferedServicePublicRead]:
    profile = await get_profile_by_user_id(session, advisor_id)
    return [_to_public(s) for s in (profile.offered_services or [])]


async def list_for_advisor(
    session: AsyncSession, advisor_id: uuid.UUID
) -> list[OfferedServiceRead]:
    profile = await get_profile_by_user_id(session, advisor_id)
    return [_to_read(s) for s in (profile.offered_services or [])]


def _build_row(
    profile_id: uuid.UUID,
    item: OfferedServiceItemInput | OfferedServiceCreateRequest,
) -> AdvisorOfferedService:
    return AdvisorOfferedService(
        profile_id=profile_id,
        service_type=item.service_type,
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
    profile = await get_profile_by_user_id(session, advisor_id)
    existing = {s.service_type for s in (profile.offered_services or [])}
    if data.service_type in existing:
        raise ConflictError(
            f"Offered service {data.service_type!r} already exists for this advisor"
        )

    row = _build_row(profile.id, data)
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
    """Admin create without an advisor — global catalog row (``profile_id`` null)."""
    existing = await session.scalar(
        select(AdvisorOfferedService.id).where(
            AdvisorOfferedService.profile_id.is_(None),
            AdvisorOfferedService.service_type == data.service_type,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"Global offered service {data.service_type!r} already exists"
        )

    row = AdvisorOfferedService(
        profile_id=None,
        service_type=data.service_type,
        price_usd=data.price_usd,
        duration_minutes=data.duration_minutes,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return _to_read(row)


async def get_row(
    session: AsyncSession, offered_service_id: uuid.UUID
) -> AdvisorOfferedService:
    row = await session.get(AdvisorOfferedService, offered_service_id)
    if row is None:
        raise NotFoundError("Offered service not found")
    return row


async def _profile_for_row(
    session: AsyncSession, row: AdvisorOfferedService
) -> AdvisorProfile:
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
    """Advisor-scoped update — service must belong to ``advisor_id``."""
    profile = await get_profile_by_user_id(session, advisor_id)
    target: AdvisorOfferedService | None = None
    for s in profile.offered_services or []:
        if s.id == offered_service_id:
            target = s
            break
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
    """Admin update — look up the row by primary key only."""
    target = await get_row(session, offered_service_id)
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
    # Blank form inputs coerce to None — treat as "leave unchanged" for
    # non-nullable columns. ``price_usd`` may be cleared to null intentionally.
    if fields.get("service_type") is None:
        fields.pop("service_type", None)
    if fields.get("duration_minutes") is None:
        fields.pop("duration_minutes", None)

    if "service_type" in fields:
        new_value = str(fields.pop("service_type"))
        if new_value != target.service_type:
            siblings = (profile.offered_services or []) if profile is not None else []
            if profile is None:
                existing = await session.scalar(
                    select(AdvisorOfferedService.id).where(
                        AdvisorOfferedService.profile_id.is_(None),
                        AdvisorOfferedService.service_type == new_value,
                        AdvisorOfferedService.id != target.id,
                    )
                )
                if existing is not None:
                    raise ConflictError(
                        f"Global offered service {new_value!r} already exists"
                    )
            else:
                dup = next(
                    (s for s in siblings if s.id != target.id and s.service_type == new_value),
                    None,
                )
                if dup is not None:
                    raise ConflictError(
                        f"Offered service {new_value!r} already exists for this advisor"
                    )
            target.service_type = new_value

    for field, value in fields.items():
        setattr(target, field, value)

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
    """Replace the advisor's offered-services collection (delete-orphan)."""
    profile = await get_profile_by_user_id(session, advisor_id)
    seen: set[str] = set()
    unique: list[OfferedServiceItemInput] = []
    for item in data.services:
        if item.service_type in seen:
            raise AppError(
                f"Duplicate service type {item.service_type!r} in request",
                code="duplicate_service_type",
            )
        seen.add(item.service_type)
        unique.append(item)

    profile.offered_services = [_build_row(profile.id, item) for item in unique]
    profile.updated_by = actor_id or advisor_id
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return [_to_read(s) for s in (profile.offered_services or [])]


async def delete(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    offered_service_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Advisor-scoped delete — service must belong to ``advisor_id``."""
    profile = await get_profile_by_user_id(session, advisor_id)
    target: AdvisorOfferedService | None = None
    for s in profile.offered_services or []:
        if s.id == offered_service_id:
            target = s
            break
    if target is None:
        raise NotFoundError("Offered service not found")

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
    """Admin delete — look up the row by primary key only."""
    target = await get_row(session, offered_service_id)
    profile = await _profile_for_row(session, target) if target.profile_id else None
    await session.delete(target)
    if profile is not None:
        profile.updated_by = actor_id
        session.add(profile)
    await session.flush()
