"""AI advisor matching — ranked shortlist after an eligibility assessment (PRD §3.4.3).

Implemented factors and weights:
- Country expertise alignment with the destination: 40
- Visa type specialization match: 30
- Years of experience (normalised against 20y cap): 15
- Average review rating (normalised against 5 stars): 15

Deferred until their subsystems exist: language preference, availability,
budget vs pricing, time zone alignment.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.advisor_profile import AdvisorProfile
from app.models.assessment import Assessment
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.assessment import AdvisorMatchRead
from app.services import review_service

COUNTRY_WEIGHT = 40.0
VISA_TYPE_WEIGHT = 30.0
EXPERIENCE_WEIGHT = 15.0
RATING_WEIGHT = 15.0
EXPERIENCE_CAP_YEARS = 20

DEFAULT_LIMIT = 5


def score_advisor_for_assessment(
    profile: AdvisorProfile,
    destination: str,
    visa_type: str,
    average_rating: float | None,
) -> float:
    """Weighted match score between an advisor's profile and an assessment's needs.

    Shared by both matching directions: ranking advisors for a seeker
    (this module) and ranking seekers as leads for an advisor
    (``advisor_lead_service``) — the factors and weights must stay identical
    between the two or "why this match" explanations would disagree.
    """
    score = 0.0
    countries = {c.country_code.upper() for c in (profile.country_expertise or [])}
    if destination.upper() in countries:
        score += COUNTRY_WEIGHT

    specializations = {s.specialization.lower() for s in (profile.visa_specializations or [])}
    if visa_type.lower() in specializations:
        score += VISA_TYPE_WEIGHT

    years = min(profile.years_of_experience or 0, EXPERIENCE_CAP_YEARS)
    score += EXPERIENCE_WEIGHT * (years / EXPERIENCE_CAP_YEARS)

    if average_rating is not None:
        score += RATING_WEIGHT * (average_rating / 5.0)
    return round(score, 2)


async def match(
    session: AsyncSession, assessment: Assessment, limit: int = DEFAULT_LIMIT
) -> list[AdvisorMatchRead]:
    """Rank approved advisors for the assessment's destination and visa type."""
    stmt = (
        select(User, AdvisorProfile)
        .join(AdvisorProfile, AdvisorProfile.user_id == User.id)
        .where(User.role == UserRole.advisor)
        .where(User.is_active.is_(True))
        .where(User.verification_status == VerificationStatus.approved)
    )
    rows = (await session.execute(stmt)).all()
    ratings = await review_service.rating_summaries(session, [user.id for user, _ in rows])

    matches = [
        AdvisorMatchRead(
            user_id=user.id,
            full_name=user.full_name,
            title=profile.title,
            profile_photo_url=profile.profile_photo_url,
            years_of_experience=profile.years_of_experience,
            match_score=score_advisor_for_assessment(
                profile,
                assessment.destination_country,
                assessment.visa_type,
                ratings[user.id][0] if user.id in ratings else None,
            ),
            public_profile_slug=profile.public_profile_slug,
        )
        for user, profile in rows
    ]
    matches = [m for m in matches if m.match_score > 0]
    matches.sort(key=lambda m: m.match_score, reverse=True)
    return matches[:limit]
