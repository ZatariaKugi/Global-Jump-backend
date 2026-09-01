"""Seeker profile data-access and business logic."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import decrypt_field, encrypt_field
from app.core.file_storage import resolve_media_url
from app.core.logging import get_logger
from app.models.advisor_profile import AdvisorOfferedService
from app.models.seeker_profile import (
    SeekerCountryVisited,
    SeekerIntendedDestination,
    SeekerIntendedVisaType,
    SeekerNeededService,
    SeekerPreferredLanguage,
    SeekerPriorVisa,
    SeekerProfile,
)
from app.models.user import User
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


async def resolve_service_types(
    session: AsyncSession, values: list[str]
) -> list[str]:
    """Normalize catalog ids / service_type slugs into unique service_type values."""
    if not values:
        return []
    cleaned = [v.strip() for v in values if v and v.strip()]
    if not cleaned:
        return []

    uuid_ids: list[uuid.UUID] = []
    slugs: list[str] = []
    for value in cleaned:
        try:
            uuid_ids.append(uuid.UUID(value))
        except ValueError:
            slugs.append(value)

    resolved: list[str] = []
    if uuid_ids:
        rows = (
            await session.execute(
                select(AdvisorOfferedService).where(AdvisorOfferedService.id.in_(uuid_ids))
            )
        ).scalars().all()
        by_id = {row.id: row.service_type for row in rows}
        for service_id in uuid_ids:
            service_type = by_id.get(service_id)
            if service_type:
                resolved.append(service_type)
            else:
                resolved.append(str(service_id))
    resolved.extend(slugs)

    seen: set[str] = set()
    unique: list[str] = []
    for service_type in resolved:
        key = service_type.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(service_type)
    return unique


async def update(
    session: AsyncSession,
    profile: SeekerProfile,
    data: SeekerProfileUpdate,
    settings: Settings,
) -> SeekerProfile:
    fields = data.model_dump(exclude_unset=True)
    # Aliases / relationship collections — never setattr raw list onto ORM columns.
    fields.pop("preferred_language", None)
    fields.pop("service_ids", None)

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

    if "preferred_languages" in fields:
        languages = fields.pop("preferred_languages") or []
        profile.preferred_languages = [
            SeekerPreferredLanguage(profile_id=profile.id, language=lang.strip())
            for lang in languages
            if lang and lang.strip()
        ]

    if "needed_services" in fields:
        raw_services = fields.pop("needed_services") or []
        service_types = await resolve_service_types(session, list(raw_services))
        profile.needed_services = [
            SeekerNeededService(profile_id=profile.id, service_type=service_type)
            for service_type in service_types
        ]

    if "intended_destinations" in fields:
        dest_codes = [
            str(c).upper()
            for c in (fields.pop("intended_destinations") or [])
            if c
        ]
        profile.intended_destinations = [
            SeekerIntendedDestination(profile_id=profile.id, country_code=code)
            for code in dest_codes
        ]
        # Keep singular primary mirror in sync (first selection).
        profile.intended_destination = dest_codes[0] if dest_codes else None
        fields.pop("intended_destination", None)
    elif "intended_destination" in fields:
        primary = fields.pop("intended_destination")
        profile.intended_destination = primary
        if primary:
            profile.intended_destinations = [
                SeekerIntendedDestination(profile_id=profile.id, country_code=str(primary).upper())
            ]
        else:
            profile.intended_destinations = []

    if "intended_visa_types" in fields:
        visa_values = [
            str(v.value if hasattr(v, "value") else v)
            for v in (fields.pop("intended_visa_types") or [])
            if v
        ]
        profile.intended_visa_types = [
            SeekerIntendedVisaType(profile_id=profile.id, visa_type=visa)
            for visa in visa_values
        ]
        profile.intended_visa_type = visa_values[0] if visa_values else None
        fields.pop("intended_visa_type", None)
    elif "intended_visa_type" in fields:
        primary_visa = fields.pop("intended_visa_type")
        visa_str = (
            None
            if primary_visa is None
            else str(primary_visa.value if hasattr(primary_visa, "value") else primary_visa)
        )
        profile.intended_visa_type = visa_str
        if visa_str:
            profile.intended_visa_types = [
                SeekerIntendedVisaType(profile_id=profile.id, visa_type=visa_str)
            ]
        else:
            profile.intended_visa_types = []

    for field, value in fields.items():
        setattr(profile, field, value)

    profile.updated_by = profile.user_id
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return profile


def preferred_language_names(profile: SeekerProfile) -> list[str]:
    """Return preferred languages as separate items.

    Also splits legacy rows that stored a comma-joined string like
    ``\"English, Spanish\"`` as a single value.
    """
    out: list[str] = []
    seen: set[str] = set()
    for row in profile.preferred_languages or []:
        if not row.language:
            continue
        for part in (p.strip() for p in row.language.split(",")):
            if not part:
                continue
            key = part.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(part)
    return out


def needed_service_types(profile: SeekerProfile) -> list[str]:
    return [row.service_type for row in (profile.needed_services or []) if row.service_type]


async def resolve_service_ids(session: AsyncSession, service_types: list[str]) -> list[str]:
    """Map stored service_type slugs back to catalog UUIDs when possible.

    Falls back to the slug itself when no global catalog row exists, so the FE
    always gets a non-empty ``service_ids`` list mirroring what was saved.
    """
    if not service_types:
        return []
    rows = (
        await session.execute(
            select(AdvisorOfferedService).where(
                AdvisorOfferedService.profile_id.is_(None),
                AdvisorOfferedService.service_type.in_(service_types),
            )
        )
    ).scalars().all()
    by_type = {row.service_type.casefold(): str(row.id) for row in rows if row.service_type}
    return [by_type.get(s.casefold(), s) for s in service_types]


def intended_destination_codes(profile: SeekerProfile) -> list[str]:
    """All intended destinations; falls back to singular column when child rows empty."""
    codes = [
        row.country_code.upper()
        for row in (profile.intended_destinations or [])
        if row.country_code
    ]
    if codes:
        return codes
    if profile.intended_destination:
        return [profile.intended_destination.upper()]
    return []


def intended_visa_type_values(profile: SeekerProfile) -> list[str]:
    """All intended visa types; falls back to singular column when child rows empty."""
    values = [row.visa_type for row in (profile.intended_visa_types or []) if row.visa_type]
    if values:
        return values
    if profile.intended_visa_type:
        return [profile.intended_visa_type]
    return []


async def build_read(
    session: AsyncSession, profile: SeekerProfile, settings: Settings
) -> SeekerProfileRead:
    """Construct ``SeekerProfileRead``, decrypting passport to a masked display value."""
    languages = preferred_language_names(profile)
    destinations = intended_destination_codes(profile)
    visa_types = intended_visa_type_values(profile)
    services = needed_service_types(profile)
    service_ids = await resolve_service_ids(session, services)
    countries = [cv.country_code for cv in (profile.countries_visited or [])]
    visas = [
        PriorVisa(country=pv.country, visa_type=pv.visa_type, year=pv.year)
        for pv in (profile.prior_visas or [])
    ]
    user = await session.get(User, profile.user_id)

    read = SeekerProfileRead(
        id=profile.id,
        user_id=profile.user_id,
        email=user.email if user is not None else None,
        date_of_birth=profile.date_of_birth,
        nationality=profile.nationality,
        profile_photo_url=resolve_media_url(profile.profile_photo_url, settings),
        banner_url=resolve_media_url(profile.banner_url, settings),
        phone=profile.phone,
        timezone=profile.timezone,
        preferred_languages=languages,
        about=profile.about,
        intended_visa_types=visa_types,
        intended_destinations=destinations,
        passport_number_masked=None,
        passport_expiry=profile.passport_expiry,
        countries_visited=countries,
        prior_visas=visas,
        service_ids=service_ids,
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
        except Exception:  # noqa: BLE001
            logger.warning("passport_decrypt_failed", profile_id=str(profile.id))
    return read
