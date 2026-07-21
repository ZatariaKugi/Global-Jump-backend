"""Advisor profile data-access and business logic."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, ConflictError, PermissionDeniedError
from app.core.file_storage import resolve_media_url
from app.core.visa_types import parse_visa_type
from app.models.advisor_credential import AdvisorCredential, DocumentType
from app.models.advisor_profile import (
    AdvisorCountryExpertise,
    AdvisorLanguage,
    AdvisorOfferedService,
    AdvisorProfile,
    AdvisorService,
    AdvisorVisaSpecialization,
)
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.review import ModerationStatus
from app.models.user import User, UserRole, VerificationStatus
from app.models.visa_type import VisaType
from app.schemas.advisor_profile import (
    AdvisorListingCard,
    AdvisorOnboardingStatusRead,
    AdvisorProfilePublicRead,
    AdvisorProfileRead,
    AdvisorProfileUpdate,
    LanguageEntry,
    ServiceOffering,
)
from app.services import review_service


def offered_service_types(profile: AdvisorProfile) -> list[str]:
    """Service-type strings for booking dropdowns.

    Prefer bookable ``AdvisorService`` types (what ``POST /bookings`` resolves),
    then any onboarding ``offered_services`` categories, de-duplicated.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in [s.service_type for s in (profile.services or [])] + [
        s.service_type for s in (profile.offered_services or [])
    ]:
        if raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out


def _visa_specializations(profile: AdvisorProfile) -> list[VisaType]:
    out: list[VisaType] = []
    for row in profile.visa_specializations or []:
        parsed = parse_visa_type(row.specialization)
        if parsed is not None:
            out.append(parsed)
    return out


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

    if "public_profile_slug" in fields:
        slug = fields["public_profile_slug"]
        if slug is None:
            # Explicit null means "leave unchanged", not clear the slug.
            fields.pop("public_profile_slug")
        else:
            taken = await session.scalar(
                select(AdvisorProfile.id).where(
                    AdvisorProfile.public_profile_slug == slug,
                    AdvisorProfile.id != profile.id,
                )
            )
            if taken is not None:
                raise ConflictError("Public profile slug is already taken")

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

    if "offered_services" in fields:
        fields.pop("offered_services")
        profile.offered_services = [
            AdvisorOfferedService(profile_id=profile.id, service_type=str(s))
            for s in (data.offered_services or [])
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
                service_type=str(svc.service_type),
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


def _build_common(profile: AdvisorProfile, settings: Settings) -> dict[str, object]:
    return {
        "title": profile.title,
        "bio": profile.bio,
        "profile_photo_url": resolve_media_url(profile.profile_photo_url, settings),
        "banner_url": resolve_media_url(profile.banner_url, settings),
        "country_of_residence": profile.country_of_residence,
        "expertise_description": profile.expertise_description,
        "years_of_experience": profile.years_of_experience,
        "successful_applications": profile.successful_applications,
        "successful_application_rate": profile.successful_application_rate,
        "offered_services": offered_service_types(profile),
        "visa_specializations": _visa_specializations(profile),
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


async def compute_avg_response_time_hours(
    session: AsyncSession, advisor_id: uuid.UUID
) -> float | None:
    """Mean hours from a seeker message to the next advisor reply across threads.

    Returns ``None`` when there are no measurable seeker→advisor reply pairs.
    """
    conv_ids = (
        await session.execute(
            select(Conversation.id, Conversation.seeker_id).where(
                Conversation.advisor_id == advisor_id
            )
        )
    ).all()
    if not conv_ids:
        return None

    seeker_by_conv = {row[0]: row[1] for row in conv_ids}
    messages = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id.in_(seeker_by_conv.keys()))
            .where(Message.deleted_at.is_(None))
            .where(Message.moderation_status != ModerationStatus.removed)
            .order_by(Message.conversation_id, Message.created_at)
        )
    ).scalars().all()

    gaps: list[float] = []
    prev_by_conv: dict[uuid.UUID, Message] = {}
    for msg in messages:
        prev = prev_by_conv.get(msg.conversation_id)
        seeker_id = seeker_by_conv[msg.conversation_id]
        if (
            prev is not None
            and prev.sender_id == seeker_id
            and msg.sender_id == advisor_id
        ):
            gaps.append((msg.created_at - prev.created_at).total_seconds() / 3600.0)
        prev_by_conv[msg.conversation_id] = msg

    if not gaps:
        return None
    return round(sum(gaps) / len(gaps), 2)


def build_read(
    profile: AdvisorProfile,
    settings: Settings,
    *,
    user: User | None = None,
    average_rating: float | None = None,
    review_count: int = 0,
    avg_response_time_hours: float | None = None,
    match_percentage: int | None = None,
) -> AdvisorProfileRead:
    return AdvisorProfileRead(
        id=profile.id,
        user_id=profile.user_id,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        average_rating=average_rating,
        review_count=review_count,
        avg_response_time_hours=avg_response_time_hours,
        verification_status=user.verification_status if user is not None else None,
        match_percentage=match_percentage,
        **_build_common(profile, settings),
    )


async def build_enriched_read(
    session: AsyncSession,
    profile: AdvisorProfile,
    user: User,
    settings: Settings,
    *,
    match_percentage: int | None = None,
) -> AdvisorProfileRead:
    """Profile read with rating, response-time, and verification badges."""
    average_rating, review_count = await review_service.rating_summary(session, profile.user_id)
    response_hours = await compute_avg_response_time_hours(session, profile.user_id)
    return build_read(
        profile,
        settings,
        user=user,
        average_rating=average_rating,
        review_count=review_count,
        avg_response_time_hours=response_hours,
        match_percentage=match_percentage,
    )


def build_listing_card(
    user: User,
    profile: AdvisorProfile | None,
    settings: Settings,
    rating: tuple[float, int] | None = None,
    match_percentage: int | None = None,
    is_bookmarked: bool = False,
    conversation_id: uuid.UUID | None = None,
) -> AdvisorListingCard:
    average_rating, review_count = rating if rating else (None, 0)
    if profile is None:
        return AdvisorListingCard(
            user_id=user.id,
            full_name=user.full_name,
            email=user.email,
            title=None,
            profile_photo_url=None,
            years_of_experience=None,
            offered_services=[],
            visa_specializations=[],
            country_expertise=[],
            languages=[],
            starting_price_usd=None,
            average_rating=average_rating,
            review_count=review_count,
            is_featured=False,
            public_profile_slug=None,
            match_percentage=match_percentage,
            is_bookmarked=is_bookmarked,
            conversation_id=conversation_id,
        )
    prices = [s.price_usd for s in (profile.services or [])]
    return AdvisorListingCard(
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
        title=profile.title,
        profile_photo_url=resolve_media_url(profile.profile_photo_url, settings),
        years_of_experience=profile.years_of_experience,
        offered_services=offered_service_types(profile),
        visa_specializations=_visa_specializations(profile),
        country_expertise=[c.country_code for c in (profile.country_expertise or [])],
        languages=[lang.language for lang in (profile.languages or [])],
        starting_price_usd=min(prices) if prices else None,
        average_rating=average_rating,
        review_count=review_count,
        is_featured=profile.is_featured,
        public_profile_slug=profile.public_profile_slug,
        match_percentage=match_percentage,
        is_bookmarked=is_bookmarked,
        conversation_id=conversation_id,
    )


def build_public_read(
    user: User,
    profile: AdvisorProfile | None,
    settings: Settings,
    match_percentage: int | None = None,
    is_bookmarked: bool = False,
) -> AdvisorProfilePublicRead:
    if profile is not None:
        return AdvisorProfilePublicRead(
            user_id=user.id,
            full_name=user.full_name,
            match_percentage=match_percentage,
            is_bookmarked=is_bookmarked,
            **_build_common(profile, settings),
        )
    return AdvisorProfilePublicRead(
        user_id=user.id,
        full_name=user.full_name,
        title=None,
        bio=None,
        profile_photo_url=None,
        banner_url=None,
        country_of_residence=None,
        expertise_description=None,
        years_of_experience=None,
        successful_applications=None,
        successful_application_rate=None,
        offered_services=[],
        visa_specializations=[],
        country_expertise=[],
        languages=[],
        services=[],
        is_featured=False,
        public_profile_slug=None,
        match_percentage=match_percentage,
        is_bookmarked=is_bookmarked,
    )


async def build_onboarding_status(
    session: AsyncSession,
    user: User,
    profile: AdvisorProfile,
) -> AdvisorOnboardingStatusRead:
    """Checklist for the Approval Pending / Status Tracking screen."""
    result = await session.execute(
        select(AdvisorCredential).where(
            AdvisorCredential.user_id == user.id,
            AdvisorCredential.is_archived.is_(False),
        )
    )
    credentials = list(result.scalars().all())
    doc_types = {c.document_type for c in credentials}
    # Treat legacy immigration_license as license for the checklist.
    has_license = DocumentType.license in doc_types or DocumentType.immigration_license in doc_types

    profile_completed = bool(
        (profile.bio and profile.bio.strip()) or profile.years_of_experience is not None
    )
    area_of_expertise_completed = bool(profile.visa_specializations) or bool(
        profile.expertise_description and profile.expertise_description.strip()
    )

    return AdvisorOnboardingStatusRead(
        verification_status=user.verification_status,
        area_of_expertise_completed=area_of_expertise_completed,
        profile_completed=profile_completed,
        government_id_uploaded=DocumentType.government_id in doc_types,
        license_uploaded=has_license,
        certification_uploaded=DocumentType.certification in doc_types,
    )


async def mark_under_review(session: AsyncSession, user: User) -> User:
    """Move advisor to ``under_review`` after onboarding submit or resubmit.

    Allowed from ``pending`` (first submit) or ``rejected`` (re-application).
    Keeps login credentials valid.
    """
    if user.verification_status in (
        None,
        VerificationStatus.pending,
        VerificationStatus.rejected,
    ):
        user.verification_status = VerificationStatus.under_review
        user.is_active = True
        user.updated_by = user.id
        session.add(user)
        await session.flush()
        await session.refresh(user)
    return user


async def resubmit_verification(session: AsyncSession, user: User) -> User:
    """Rejected advisor re-applies for account review → ``under_review``.

    Does not change password or revoke tokens. Advisor should upload any
    updated credentials via ``POST /advisors/me/credentials`` (or re-run
    onboarding) so they reappear on the admin verification queue.
    """
    if user.role != UserRole.advisor:
        raise PermissionDeniedError("Advisor account required")
    if user.verification_status != VerificationStatus.rejected:
        raise AppError(
            "Only a rejected application can be resubmitted",
            code="invalid_verification_state",
        )
    return await mark_under_review(session, user)
