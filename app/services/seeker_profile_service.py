"""Seeker profile data-access and business logic."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import decrypt_field, encrypt_field
from app.core.logging import get_logger
from app.models.seeker_profile import (
    SeekerCountryVisited,
    SeekerPriorVisa,
    SeekerProfile,
)
from app.schemas.seeker_profile import PriorVisa, SeekerProfileRead, SeekerProfileUpdate

logger = get_logger(__name__)


async def get_by_user_id(session: AsyncSession, user_id: uuid.UUID) -> SeekerProfile | None:
    result = await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == user_id))
    return result.scalar_one_or_none()


async def get_or_create(session: AsyncSession, user_id: uuid.UUID) -> SeekerProfile:
    profile = await get_by_user_id(session, user_id)
    if profile is None:
        profile = SeekerProfile(user_id=user_id)
        session.add(profile)
        await session.flush()
        await session.refresh(profile)
    return profile


async def update(
    session: AsyncSession,
    profile: SeekerProfile,
    data: SeekerProfileUpdate,
    settings: Settings,
) -> SeekerProfile:
    fields = data.model_dump(exclude_unset=True)

    raw_passport = fields.pop("passport_number", None)
    if raw_passport is not None:
        profile.passport_number_encrypted = encrypt_field(raw_passport, settings)

    if "countries_visited" in fields:
        codes = fields.pop("countries_visited") or []
        profile.countries_visited = [
            SeekerCountryVisited(profile_id=profile.id, country_code=c) for c in codes
        ]

    if "prior_visas" in fields:
        fields.pop("prior_visas")
        profile.prior_visas = [
            SeekerPriorVisa(
                profile_id=profile.id,
                country=v.country,
                visa_type=v.visa_type,
                year=v.year,
            )
            for v in (data.prior_visas or [])
        ]

    for field, value in fields.items():
        setattr(profile, field, value)

    profile.updated_by = profile.user_id
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return profile


def build_read(profile: SeekerProfile, settings: Settings) -> SeekerProfileRead:
    """Construct ``SeekerProfileRead``, decrypting passport to a masked display value."""
    countries = [cv.country_code for cv in (profile.countries_visited or [])]
    visas = [
        PriorVisa(country=pv.country, visa_type=pv.visa_type, year=pv.year)
        for pv in (profile.prior_visas or [])
    ]

    read = SeekerProfileRead(
        id=profile.id,
        user_id=profile.user_id,
        date_of_birth=profile.date_of_birth,
        nationality=profile.nationality,
        country_of_residence=profile.country_of_residence,
        profile_photo_url=profile.profile_photo_url,
        intended_visa_type=profile.intended_visa_type,
        intended_destination=profile.intended_destination,
        passport_number_masked=None,
        passport_expiry=profile.passport_expiry,
        countries_visited=countries,
        prior_visas=visas,
        education_level=profile.education_level,
        employment_status=profile.employment_status,
        employer_name=profile.employer_name,
        annual_income_band=profile.annual_income_band,
        has_bank_statements=profile.has_bank_statements,
        email_notifications=profile.email_notifications,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )

    if profile.passport_number_encrypted:
        try:
            raw = decrypt_field(profile.passport_number_encrypted, settings)
            read = read.model_copy(
                update={"passport_number_masked": raw[-4:] if len(raw) >= 4 else raw}
            )
        except Exception:
            logger.warning("passport_decryption_failed", user_id=str(profile.user_id))

    return read
