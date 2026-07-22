"""AI advisor matching — ranked shortlist after an eligibility assessment (PRD §3.4.3).

Weights are admin-configurable via ``advisor_matching_weights`` (UI sliders):
- country_weight — destination expertise
- language_weight — advisor has languages configured
- availability_weight — advisor has weekly slots
- setting_weight — visa-type specialization (UI "Setting")
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.visa_types import parse_visa_type
from app.models.advisor_availability import AdvisorWeeklySlot
from app.models.advisor_profile import AdvisorProfile
from app.models.assessment import Assessment, AssessmentStatus
from app.models.seeker_profile import SeekerProfile
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.assessment import AdvisorMatchRead
from app.services import matching_weights_service, review_service
from app.services.matching_weights_service import DEFAULT_CONFIG, MatchingWeightConfig

DEFAULT_LIMIT = 5


def score_advisor_for_assessment(
    profile: AdvisorProfile,
    destination: str,
    visa_type: str,
    average_rating: float | None,
    *,
    weights: MatchingWeightConfig = DEFAULT_CONFIG,
    has_availability: bool = False,
) -> float:
    """Weighted match score between an advisor's profile and an assessment's needs."""
    score = 0.0
    countries = {c.country_code.upper() for c in (profile.country_expertise or [])}
    if destination.upper() in countries:
        score += weights.country

    if profile.languages:
        score += weights.language

    if has_availability:
        score += weights.availability

    specializations = {
        parsed
        for s in (profile.visa_specializations or [])
        if (parsed := parse_visa_type(s.specialization)) is not None
    }
    target = parse_visa_type(visa_type)
    if target is not None and target in specializations:
        # Setting weight = visa pathway fit; rating nudges within that band.
        setting = weights.setting
        if average_rating is not None:
            score += setting * (0.7 + 0.3 * (average_rating / 5.0))
        else:
            score += setting * 0.7
    return round(min(score, 100.0), 2)


async def match_context_for_seeker(
    session: AsyncSession, seeker_id: uuid.UUID
) -> tuple[str | None, str | None]:
    """Destination + visa type from latest completed assessment, else seeker profile."""
    assessment = (
        await session.execute(
            select(Assessment)
            .where(
                Assessment.user_id == seeker_id,
                Assessment.status == AssessmentStatus.completed,
            )
            .order_by(Assessment.completed_at.desc().nulls_last(), Assessment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if assessment is not None:
        return assessment.destination_country, assessment.visa_type

    profile = (
        await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == seeker_id))
    ).scalar_one_or_none()
    if profile is None:
        return None, None
    return profile.intended_destination, profile.intended_visa_type


def match_percentage(
    profile: AdvisorProfile | None,
    destination: str | None,
    visa_type: str | None,
    average_rating: float | None,
    *,
    weights: MatchingWeightConfig = DEFAULT_CONFIG,
    has_availability: bool = False,
) -> int | None:
    """0–100 match for seeker-facing advisor cards; ``None`` without destination/visa."""
    if profile is None or not destination or not visa_type:
        return None
    return int(
        round(
            score_advisor_for_assessment(
                profile,
                destination,
                visa_type,
                average_rating,
                weights=weights,
                has_availability=has_availability,
            )
        )
    )


async def match(
    session: AsyncSession,
    assessment: Assessment,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[AdvisorMatchRead], int]:
    """Rank approved advisors for the assessment's destination and visa type.

    Returns ``(page_items, total_matching)`` so callers can embed a short list
    on the assessment result or paginate via a dedicated endpoint.
    """
    weights = await matching_weights_service.get_config(session)
    stmt = (
        select(User, AdvisorProfile)
        .join(AdvisorProfile, AdvisorProfile.user_id == User.id)
        .where(User.role == UserRole.advisor)
        .where(User.is_active.is_(True))
        .where(User.verification_status == VerificationStatus.approved)
    )
    rows = (await session.execute(stmt)).all()
    advisor_ids = [user.id for user, _ in rows]
    ratings = await review_service.rating_summaries(session, advisor_ids)

    available: set[uuid.UUID] = set()
    if advisor_ids:
        slot_rows = (
            await session.execute(
                select(AdvisorWeeklySlot.advisor_id)
                .where(AdvisorWeeklySlot.advisor_id.in_(advisor_ids))
                .distinct()
            )
        ).all()
        available = {row[0] for row in slot_rows}

    matches = [
        AdvisorMatchRead(
            user_id=user.id,
            full_name=user.full_name,
            email=user.email,
            title=profile.title,
            profile_photo_url=profile.profile_photo_url,
            years_of_experience=profile.years_of_experience,
            average_rating=ratings[user.id][0] if user.id in ratings else None,
            match_score=score_advisor_for_assessment(
                profile,
                assessment.destination_country,
                assessment.visa_type,
                ratings[user.id][0] if user.id in ratings else None,
                weights=weights,
                has_availability=user.id in available,
            ),
            public_profile_slug=profile.public_profile_slug,
        )
        for user, profile in rows
    ]
    matches = [m for m in matches if m.match_score > 0]
    matches.sort(key=lambda m: m.match_score, reverse=True)
    total = len(matches)
    if limit <= 0:
        return [], total
    return matches[offset : offset + limit], total
