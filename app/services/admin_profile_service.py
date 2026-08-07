"""Admin profile service."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.file_storage import resolve_media_url
from app.models.admin_profile import AdminProfile
from app.models.user import User
from app.schemas.admin_profile import AdminProfileRead, AdminProfileUpdate


async def get_by_user_id(
    session: AsyncSession, user_id: uuid.UUID
) -> AdminProfile | None:
    result = await session.execute(
        select(AdminProfile).where(AdminProfile.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_or_create(
    session: AsyncSession, user_id: uuid.UUID
) -> AdminProfile:
    profile = await get_by_user_id(session, user_id)
    if profile is None:
        profile = AdminProfile(user_id=user_id)
        session.add(profile)
        await session.flush()
        await session.refresh(profile)
    return profile


async def update(
    session: AsyncSession,
    profile: AdminProfile,
    data: AdminProfileUpdate,
) -> AdminProfile:
    fields = data.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(profile, field, value)
    profile.updated_by = profile.user_id
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return profile


def build_read(
    profile: AdminProfile, user: User, settings: Settings
) -> AdminProfileRead:
    photo = resolve_media_url(profile.profile_photo_url, settings)
    banner = resolve_media_url(profile.banner_url, settings)
    return AdminProfileRead(
        id=profile.id,
        user_id=profile.user_id,
        full_name=user.full_name,
        email=user.email,
        title=profile.title,
        phone=profile.phone,
        country_of_residence=profile.country_of_residence,
        timezone=profile.timezone,
        preferred_language=profile.preferred_language,
        about=profile.about,
        profile_photo_url=photo,
        banner_url=banner,
        is_active=user.is_active,
        is_email_verified=user.email_verified_at is not None,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )
