"""Advisor profile data-access and business logic."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.advisor_profile import (
    AdvisorCountryExpertise,
    AdvisorLanguage,
    AdvisorProfile,
    AdvisorService,
    AdvisorVisaSpecialization,
)
from app.models.user import User
from app.schemas.advisor_profile import (
    AdvisorListingCard,
    AdvisorProfilePublicRead,
    AdvisorProfileRead,
    AdvisorProfileUpdate,
    LanguageEntry,
    ServiceOffering,
)


async def get_by_user_id(session: AsyncSession, user_id: uuid.UUID) -> AdvisorProfile | None:
    result = await session.execute(select(AdvisorProfile).where(AdvisorProfile.user_id == user_id))
    return result.scalar_one_or_none()


async def get_or_create(session: AsyncSession, user_id: uuid.UUID) -> AdvisorProfile:
    profile = await get_by_user_id(session, user_id)
    if profile is None:
        profile = AdvisorProfile(user_id=user_id)
        session.add(profile)
        await session.flush()
        await session.refresh(profile)
    return profile


async def update(
    session: AsyncSession,
    profile: AdvisorProfile,
    data: AdvisorProfileUpdate,
) -> AdvisorProfile:
    fields = data.model_dump(exclude_unset=True)

    if "visa_specializations" in fields:
        fields.pop("visa_specializations")
        profile.visa_specializations = [
            AdvisorVisaSpecialization(profile_id=profile.id, specialization=str(s))
            for s in (data.visa_specializations or [])
        ]

    if "country_expertise" in fields:
        codes = fields.pop("country_expertise") or []
        profile.country_expertise = [
            AdvisorCountryExpertise(profile_id=profile.id, country_code=c) for c in codes
        ]

    if "languages" in fields:
        fields.pop("languages")
        profile.languages = [
            AdvisorLanguage(
                profile_id=profile.id,
                language=lang.language,
                proficiency=lang.proficiency,
            )
            for lang in (data.languages or [])
        ]

    if "services" in fields:
        fields.pop("services")
        profile.services = [
            AdvisorService(
                profile_id=profile.id,
                service_type=svc.service_type,
                duration_minutes=svc.duration_minutes,
                price_usd=svc.price_usd,
            )
            for svc in (data.services or [])
        ]

    for field, value in fields.items():
        setattr(profile, field, value)

    profile.updated_by = profile.user_id
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return profile


def _build_common(profile: AdvisorProfile) -> dict[str, object]:
    return {
        "title": profile.title,
        "bio": profile.bio,
        "profile_photo_url": profile.profile_photo_url,
        "years_of_experience": profile.years_of_experience,
        "successful_applications": profile.successful_applications,
        "successful_application_rate": profile.successful_application_rate,
        "visa_specializations": [s.specialization for s in (profile.visa_specializations or [])],
        "country_expertise": [c.country_code for c in (profile.country_expertise or [])],
        "languages": [
            LanguageEntry(language=lang.language, proficiency=lang.proficiency)
            for lang in (profile.languages or [])
        ],
        "services": [
            ServiceOffering(
                service_type=s.service_type,
                duration_minutes=s.duration_minutes,
                price_usd=s.price_usd,
            )
            for s in (profile.services or [])
        ],
        "is_featured": profile.is_featured,
        "public_profile_slug": profile.public_profile_slug,
    }


def build_read(profile: AdvisorProfile) -> AdvisorProfileRead:
    return AdvisorProfileRead(
        id=profile.id,
        user_id=profile.user_id,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        **_build_common(profile),
    )


def build_listing_card(
    user: User,
    profile: AdvisorProfile | None,
    rating: tuple[float, int] | None = None,
) -> AdvisorListingCard:
    average_rating, review_count = rating if rating else (None, 0)
    if profile is None:
        return AdvisorListingCard(
            user_id=user.id,
            full_name=user.full_name,
            title=None,
            profile_photo_url=None,
            years_of_experience=None,
            visa_specializations=[],
            country_expertise=[],
            languages=[],
            starting_price_usd=None,
            average_rating=average_rating,
            review_count=review_count,
            is_featured=False,
            public_profile_slug=None,
        )
    prices = [s.price_usd for s in (profile.services or [])]
    return AdvisorListingCard(
        user_id=user.id,
        full_name=user.full_name,
        title=profile.title,
        profile_photo_url=profile.profile_photo_url,
        years_of_experience=profile.years_of_experience,
        visa_specializations=[s.specialization for s in (profile.visa_specializations or [])],
        country_expertise=[c.country_code for c in (profile.country_expertise or [])],
        languages=[lang.language for lang in (profile.languages or [])],
        starting_price_usd=min(prices) if prices else None,
        average_rating=average_rating,
        review_count=review_count,
        is_featured=profile.is_featured,
        public_profile_slug=profile.public_profile_slug,
    )


def build_public_read(user: User, profile: AdvisorProfile | None) -> AdvisorProfilePublicRead:
    if profile is not None:
        return AdvisorProfilePublicRead(
            user_id=user.id,
            full_name=user.full_name,
            **_build_common(profile),
        )
    return AdvisorProfilePublicRead(
        user_id=user.id,
        full_name=user.full_name,
        title=None,
        bio=None,
        profile_photo_url=None,
        years_of_experience=None,
        successful_applications=None,
        successful_application_rate=None,
        visa_specializations=[],
        country_expertise=[],
        languages=[],
        services=[],
        is_featured=False,
        public_profile_slug=None,
    )
