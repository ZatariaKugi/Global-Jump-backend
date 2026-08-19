"""Persist and serve profile-based Find Advisor recommendations.

``GET /advisors?recommended=true`` and dashboard ``matched_advisors`` read these
rows. They are regenerated from seeker profile intent only (onboarding /
profile destination+visa) — never from AI Assessment. Assessment matches live
in ``advisor_leads`` and ``GET /assessments/{id}/matched-advisors``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.file_storage import resolve_media_url
from app.models.advisor_profile import AdvisorProfile
from app.models.seeker_advisor_recommendation import SeekerAdvisorRecommendation
from app.models.user import User
from app.models.visa_type import VisaType
from app.schemas.assessment import AdvisorMatchRead
from app.services import advisor_matching_service, advisor_profile_service, review_service
from app.services.advisor_profile_service import build_match_reasons
from app.services.ai_advisor_match_service import SeekerMatchCase

PROFILE_CONTEXT = "profile"


async def list_for_seeker(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    destination: str | None = None,
    visa_type: str | None = None,
) -> list[SeekerAdvisorRecommendation]:
    """Saved profile-based recommendations ordered by rank (best first)."""
    stmt = (
        select(SeekerAdvisorRecommendation)
        .where(
            SeekerAdvisorRecommendation.seeker_id == seeker_id,
            SeekerAdvisorRecommendation.is_archived.is_(False),
            SeekerAdvisorRecommendation.context_source == PROFILE_CONTEXT,
        )
        .order_by(
            SeekerAdvisorRecommendation.rank.asc(),
            SeekerAdvisorRecommendation.match_score.desc(),
        )
    )
    if destination:
        stmt = stmt.where(
            SeekerAdvisorRecommendation.destination_country == destination.upper()
        )
    if visa_type:
        stmt = stmt.where(SeekerAdvisorRecommendation.visa_type == visa_type)
    return list((await session.execute(stmt)).scalars().all())


async def clear_for_seeker(session: AsyncSession, seeker_id: uuid.UUID) -> None:
    """Remove profile-cache rows only — never advisor_leads or other contexts."""
    await session.execute(
        delete(SeekerAdvisorRecommendation).where(
            SeekerAdvisorRecommendation.seeker_id == seeker_id,
            SeekerAdvisorRecommendation.context_source == PROFILE_CONTEXT,
        )
    )


async def replace_from_matches(
    session: AsyncSession,
    case: SeekerMatchCase,
    matches: list[AdvisorMatchRead],
    *,
    actor_id: uuid.UUID | None = None,
) -> list[SeekerAdvisorRecommendation]:
    """Replace the seeker's profile recommendation set from a hybrid match result."""
    if case.seeker_id is None:
        return []
    if case.context_source != PROFILE_CONTEXT:
        return []

    await clear_for_seeker(session, case.seeker_id)

    # Batch-load all advisor profiles to avoid N+1 queries.
    advisor_ids = [item.user_id for item in matches]
    profiles_by_id: dict[uuid.UUID, AdvisorProfile] = {}
    if advisor_ids:
        profile_rows = (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id.in_(advisor_ids))
            )
        ).scalars().all()
        profiles_by_id = {p.user_id: p for p in profile_rows}

    rows: list[SeekerAdvisorRecommendation] = []
    for rank, item in enumerate(matches, start=1):
        reasons = item.match_reasons
        if not reasons:
            profile = profiles_by_id.get(item.user_id)
            reasons = (
                build_match_reasons(
                    profile,
                    case.destination_country,
                    case.visa_type,
                    item.average_rating,
                )
                if profile is not None
                else "Matched by recommendation engine"
            )
        row = SeekerAdvisorRecommendation(
            seeker_id=case.seeker_id,
            advisor_id=item.user_id,
            assessment_id=None,
            destination_country=case.destination_country.upper(),
            visa_type=case.visa_type,
            context_source=PROFILE_CONTEXT,
            match_score=item.match_score,
            rule_score=item.rule_score,
            ai_score=item.ai_score,
            match_reasons=reasons[:1000],
            rank=rank,
            created_by=actor_id or case.seeker_id,
            updated_by=actor_id or case.seeker_id,
        )
        session.add(row)
        rows.append(row)

    if rows:
        await session.flush()
        for row in rows:
            await session.refresh(row)

    return rows


async def refresh_for_seeker(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    use_ai: bool = True,
) -> list[SeekerAdvisorRecommendation]:
    """Recompute hybrid matches from profile intent and persist them."""
    case = await advisor_matching_service.build_profile_match_case(session, seeker_id)
    if case is None:
        await clear_for_seeker(session, seeker_id)
        return []

    ranked, _total = await advisor_matching_service.match_from_context(
        session,
        case,
        limit=500,
        offset=0,
        positive_only=True,
        settings=settings,
        use_ai=use_ai,
    )
    return await replace_from_matches(session, case, ranked, actor_id=seeker_id)


async def ensure_for_seeker(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    destination: str,
    visa_type: str,
    settings: Settings | None = None,
) -> list[SeekerAdvisorRecommendation]:
    """Return saved profile rows for the current intent, refreshing when missing/stale."""
    existing = await list_for_seeker(
        session,
        seeker_id,
        destination=destination,
        visa_type=visa_type,
    )
    if existing:
        return existing

    return await refresh_for_seeker(session, seeker_id, settings=settings)


async def as_match_reads(
    session: AsyncSession,
    recs: list[SeekerAdvisorRecommendation],
    settings: Settings,
) -> list[AdvisorMatchRead]:
    """Hydrate saved profile rows into the dashboard / match-card shape."""
    if not recs:
        return []
    advisor_ids = [r.advisor_id for r in recs]
    users = {
        u.id: u
        for u in (
            await session.execute(select(User).where(User.id.in_(advisor_ids)))
        ).scalars().all()
    }
    profiles = {
        p.user_id: p
        for p in (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id.in_(advisor_ids))
            )
        ).scalars().all()
    }
    ratings = await review_service.rating_summaries(session, advisor_ids)
    reads: list[AdvisorMatchRead] = []
    for rec in recs:
        user = users.get(rec.advisor_id)
        if user is None:
            continue
        profile = profiles.get(rec.advisor_id)
        avg, _count = ratings.get(rec.advisor_id, (None, 0))
        reads.append(
            AdvisorMatchRead(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                title=profile.title if profile is not None else None,
                profile_photo_url=(
                    resolve_media_url(profile.profile_photo_url, settings)
                    if profile is not None
                    else None
                ),
                years_of_experience=(
                    profile.years_of_experience if profile is not None else None
                ),
                average_rating=avg,
                starting_price_usd=advisor_profile_service.starting_price_usd(profile),
                match_score=rec.match_score,
                public_profile_slug=(
                    profile.public_profile_slug if profile is not None else None
                ),
                match_reasons=rec.match_reasons,
                rule_score=rec.rule_score,
                ai_score=rec.ai_score,
            )
        )
    return reads


async def matches_for_dashboard(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    settings: Settings,
    *,
    visa_type: VisaType | None = None,
    country: str | None = None,
    limit: int = 10,
) -> list[AdvisorMatchRead]:
    """Dashboard ``matched_advisors`` from ``seeker_advisor_recommendations``.

    Refreshes the profile cache on a full miss. Query ``visa_type`` / ``country``
    filter saved rows; they never pull AI Assessment matches.
    """
    existing = await list_for_seeker(session, seeker_id)
    if not existing:
        existing = await refresh_for_seeker(session, seeker_id, settings=settings)
    recs = existing
    if country is not None:
        dest = country.upper()
        recs = [r for r in recs if r.destination_country == dest]
    if visa_type is not None:
        recs = [r for r in recs if r.visa_type == visa_type.value]
    return await as_match_reads(session, recs[:limit], settings)
