"""AI-matched customer leads for advisors — the inverse of advisor_matching_service.

Generated once, when a seeker's eligibility assessment completes
(``assessment_service.submit_answers``): uses the hybrid matcher
(country gate + rule score + optional OpenAI re-rank blend) and persists
``AdvisorLead`` rows for positive matches. Seekers read that snapshot on
history / ``GET /assessments/{id}/matched-advisors`` (no OpenAI). Advisors
then work this list as a queue (new -> viewed -> contacted or dismissed)
via ``GET/POST /advisors/me/leads...``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError
from app.core.file_storage import resolve_media_url
from app.models.advisor_lead import AdvisorLead, AdvisorLeadStatus
from app.models.advisor_profile import AdvisorProfile
from app.models.assessment import Assessment
from app.models.booking import Booking
from app.models.user import User
from app.schemas.assessment import AdvisorMatchRead
from app.services import advisor_matching_service, advisor_profile_service, review_service
from app.services.advisor_profile_service import build_match_reasons


async def generate_for_assessment(
    session: AsyncSession,
    assessment: Assessment,
) -> list[AdvisorLead]:
    """Persist leads from the hybrid matcher (rule score + optional AI blend)."""
    ranked, _total = await advisor_matching_service.match(
        session,
        assessment,
        limit=500,
        offset=0,
        positive_only=True,
        use_ai=True,
    )

    leads: list[AdvisorLead] = []

    # Batch-load advisor profiles to avoid N+1 queries.
    advisor_ids = [item.user_id for item in ranked]
    profiles_by_id: dict[uuid.UUID, AdvisorProfile] = {}
    if advisor_ids:
        profile_rows = (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id.in_(advisor_ids))
            )
        ).scalars().all()
        profiles_by_id = {p.user_id: p for p in profile_rows}

    for item in ranked:
        reasons = item.match_reasons
        if not reasons:
            profile = profiles_by_id.get(item.user_id)
            reasons = (
                build_match_reasons(
                    profile,
                    assessment.destination_country,
                    assessment.visa_type,
                    item.average_rating,
                )
                if profile is not None
                else "Matched by recommendation engine"
            )
        lead = AdvisorLead(
            seeker_id=assessment.user_id,
            advisor_id=item.user_id,
            assessment_id=assessment.id,
            match_score=item.match_score,
            match_reasons=reasons,
            status=AdvisorLeadStatus.new,
        )
        session.add(lead)
        leads.append(lead)

    if leads:
        await session.flush()
        for lead in leads:
            await session.refresh(lead)
    return leads


def list_for_assessment_stmt(assessment_id: uuid.UUID) -> Select[tuple[AdvisorLead]]:
    """Persisted assessment matches for the seeker history / match panel."""
    return (
        select(AdvisorLead)
        .where(
            AdvisorLead.assessment_id == assessment_id,
            AdvisorLead.is_archived.is_(False),
        )
        .order_by(AdvisorLead.match_score.desc(), AdvisorLead.created_at.desc())
    )


async def counts_for_assessments(
    session: AsyncSession, assessment_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Per-assessment lead counts — no live matcher / OpenAI."""
    if not assessment_ids:
        return {}
    stmt = (
        select(AdvisorLead.assessment_id, func.count())
        .where(
            AdvisorLead.assessment_id.in_(assessment_ids),
            AdvisorLead.is_archived.is_(False),
        )
        .group_by(AdvisorLead.assessment_id)
    )
    return {
        assessment_id: int(count) for assessment_id, count in (await session.execute(stmt)).all()
    }


async def as_match_reads(
    session: AsyncSession,
    leads: list[AdvisorLead],
    settings: Settings | None = None,
) -> list[AdvisorMatchRead]:
    """Hydrate saved leads into the seeker match-card shape."""
    if not leads:
        return []
    cfg = settings or get_settings()
    advisor_ids = [lead.advisor_id for lead in leads]
    users = {
        user.id: user
        for user in (await session.execute(select(User).where(User.id.in_(advisor_ids))))
        .scalars()
        .all()
    }
    profiles = {
        profile.user_id: profile
        for profile in (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id.in_(advisor_ids))
            )
        )
        .scalars()
        .all()
    }
    ratings = await review_service.rating_summaries(session, advisor_ids)
    reads: list[AdvisorMatchRead] = []
    for lead in leads:
        user = users.get(lead.advisor_id)
        if user is None:
            continue
        profile = profiles.get(lead.advisor_id)
        avg, _count = ratings.get(lead.advisor_id, (None, 0))
        reads.append(
            AdvisorMatchRead(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                title=profile.title if profile is not None else None,
                profile_photo_url=(
                    resolve_media_url(profile.profile_photo_url, cfg)
                    if profile is not None
                    else None
                ),
                years_of_experience=(profile.years_of_experience if profile is not None else None),
                average_rating=avg,
                starting_price_usd=advisor_profile_service.starting_price_usd(profile),
                match_score=lead.match_score,
                public_profile_slug=(profile.public_profile_slug if profile is not None else None),
                match_reasons=lead.match_reasons,
                rule_score=None,
                ai_score=None,
            )
        )
    return reads


async def matches_for_assessment(
    session: AsyncSession,
    assessment_id: uuid.UUID,
    *,
    limit: int,
    offset: int = 0,
    settings: Settings | None = None,
) -> tuple[list[AdvisorMatchRead], int]:
    """Paginated persisted matches. ``limit <= 0`` returns only the total."""
    stmt = list_for_assessment_stmt(assessment_id)
    total = int(
        (
            await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
        ).scalar_one()
    )
    if limit <= 0:
        return [], total
    leads = list((await session.execute(stmt.offset(offset).limit(limit))).scalars().all())
    return await as_match_reads(session, leads, settings), total


def list_for_advisor_stmt(
    advisor_id: uuid.UUID,
    status: AdvisorLeadStatus | None = None,
    q: str | None = None,
    visa_type: str | None = None,
) -> Select[tuple[AdvisorLead]]:
    stmt = (
        select(AdvisorLead)
        .where(AdvisorLead.advisor_id == advisor_id)
        .order_by(AdvisorLead.match_score.desc(), AdvisorLead.created_at.desc())
    )
    if status is not None:
        stmt = stmt.where(AdvisorLead.status == status)
    if q:
        seeker = aliased(User)
        stmt = stmt.join(seeker, seeker.id == AdvisorLead.seeker_id)
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                seeker.full_name.ilike(term),
                seeker.email.ilike(term),
                AdvisorLead.match_reasons.ilike(term),
            )
        )
    if visa_type:
        stmt = stmt.join(Assessment, Assessment.id == AdvisorLead.assessment_id).where(
            Assessment.visa_type == visa_type
        )
    return stmt


async def get_for_advisor(
    session: AsyncSession, lead_id: uuid.UUID, advisor_id: uuid.UUID
) -> AdvisorLead:
    lead = await session.get(AdvisorLead, lead_id)
    if lead is None or lead.advisor_id != advisor_id:
        raise NotFoundError("Lead not found")
    return lead


async def mark_viewed(session: AsyncSession, lead: AdvisorLead) -> AdvisorLead:
    if lead.status == AdvisorLeadStatus.new:
        lead.status = AdvisorLeadStatus.viewed
        session.add(lead)
        await session.flush()
        await session.refresh(lead)
    return lead


async def mark_contacted(
    session: AsyncSession, lead: AdvisorLead, actor_id: uuid.UUID
) -> AdvisorLead:
    lead.status = AdvisorLeadStatus.contacted
    lead.updated_by = actor_id
    session.add(lead)
    await session.flush()
    await session.refresh(lead)
    return lead


async def dismiss(session: AsyncSession, lead: AdvisorLead, actor_id: uuid.UUID) -> AdvisorLead:
    lead.status = AdvisorLeadStatus.dismissed
    lead.updated_by = actor_id
    session.add(lead)
    await session.flush()
    await session.refresh(lead)
    return lead


async def latest_booking_for_pair(
    session: AsyncSession, seeker_id: uuid.UUID, advisor_id: uuid.UUID
) -> Booking | None:
    """Most recent booking between this seeker and advisor, if any."""
    result = await session.execute(
        select(Booking)
        .where(Booking.seeker_id == seeker_id, Booking.advisor_id == advisor_id)
        .order_by(Booking.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()
