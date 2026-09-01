"""Advisor service offerings — CRUD for bookable services."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.advisor_profile import AdvisorProfile, AdvisorService
from app.schemas.advisor_profile import ServiceCreateRequest, ServiceRead, ServiceUpdateRequest


async def get_profile_by_user_id(
    session: AsyncSession, user_id: uuid.UUID
) -> AdvisorProfile:
    profile = await session.scalar(
        select(AdvisorProfile).where(AdvisorProfile.user_id == user_id)
    )
    if profile is None:
        raise NotFoundError("Advisor profile not found")
    return profile


async def list_services(
    session: AsyncSession, advisor_id: uuid.UUID
) -> list[ServiceRead]:
    profile = await get_profile_by_user_id(session, advisor_id)
    return [
        ServiceRead(
            id=s.id,
            service_type=s.service_type,
            duration_minutes=s.duration_minutes,
            price_usd=s.price_usd,
        )
        for s in (profile.services or [])
    ]


async def get_service(
    session: AsyncSession, advisor_id: uuid.UUID, service_id: uuid.UUID
) -> AdvisorService:
    profile = await get_profile_by_user_id(session, advisor_id)
    for s in profile.services or []:
        if s.id == service_id:
            return s
    raise NotFoundError("Service not found")


async def create_service(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    data: ServiceCreateRequest,
    *,
    actor_id: uuid.UUID | None = None,
) -> ServiceRead:
    profile = await get_profile_by_user_id(session, advisor_id)
    service = AdvisorService(
        profile_id=profile.id,
        service_type=data.service_type.value,
        duration_minutes=data.duration_minutes,
        price_usd=data.price_usd,
    )
    session.add(service)
    profile.updated_by = actor_id or advisor_id
    session.add(profile)
    await session.flush()
    await session.refresh(service)
    return ServiceRead(
        id=service.id,
        service_type=service.service_type,
        duration_minutes=service.duration_minutes,
        price_usd=service.price_usd,
    )


async def update_service(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    service_id: uuid.UUID,
    data: ServiceUpdateRequest,
    *,
    actor_id: uuid.UUID | None = None,
) -> ServiceRead:
    profile = await get_profile_by_user_id(session, advisor_id)
    target: AdvisorService | None = None
    for s in profile.services or []:
        if s.id == service_id:
            target = s
            break
    if target is None:
        raise NotFoundError("Service not found")

    fields = data.model_dump(exclude_unset=True)
    for field, value in fields.items():
        if field == "service_type":
            setattr(target, field, value.value)
        else:
            setattr(target, field, value)

    profile.updated_by = actor_id or advisor_id
    session.add(profile)
    await session.flush()
    await session.refresh(target)
    return ServiceRead(
        id=target.id,
        service_type=target.service_type,
        duration_minutes=target.duration_minutes,
        price_usd=target.price_usd,
    )


async def delete_service(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    service_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
) -> None:
    profile = await get_profile_by_user_id(session, advisor_id)
    target: AdvisorService | None = None
    for s in profile.services or []:
        if s.id == service_id:
            target = s
            break
    if target is None:
        raise NotFoundError("Service not found")

    await session.delete(target)
    profile.updated_by = actor_id or advisor_id
    session.add(profile)
    await session.flush()
