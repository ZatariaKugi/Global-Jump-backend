"""AI-matched customer leads for advisors — the inverse of advisor_matching_service.

Generated once, when a seeker's eligibility assessment completes
(``assessment_service.submit_answers``): every approved+active advisor is scored
against the assessment using the *same* weighted factors as
``advisor_matching_service.score_advisor_for_assessment`` (country/visa/experience/
rating), and a persisted ``AdvisorLead`` row is created for any advisor with a
positive score. Advisors then work this list as a queue (new -> viewed -> contacted
or dismissed) via ``GET/POST /advisors/me/leads...``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.visa_types import parse_visa_type, visa_type_name
from app.models.advisor_lead import AdvisorLead, AdvisorLeadStatus
from app.models.advisor_profile import AdvisorProfile
from app.models.assessment import Assessment
from app.models.booking import Booking
from app.models.user import User, UserRole, VerificationStatus
from app.services import review_service
from app.services.advisor_matching_service import score_advisor_for_assessment


def _build_reasons(
    profile: AdvisorProfile,
    destination: str,
    visa_type: str,
    average_rating: float | None,
) -> str:
    reasons: list[str] = []
    countries = {c.country_code.upper() for c in (profile.country_expertise or [])}
    if destination.upper() in countries:
        reasons.append(f"Specializes in {destination.upper()} immigration")

    specializations = {
        parsed
        for s in (profile.visa_specializations or [])
        if (parsed := parse_visa_type(s.specialization)) is not None
    }
    target = parse_visa_type(visa_type)
    if target is not None and target in specializations:
        label = visa_type_name(target) or target.value
        reasons.append(f"Specializes in {label}")

    if profile.years_of_experience:
        reasons.append(f"{profile.years_of_experience} years of experience")

    if average_rating is not None:
        reasons.append(f"{average_rating:.1f}★ average rating")

    return "; ".join(reasons) if reasons else "General profile match"


async def generate_for_assessment(
    session: AsyncSession, assessment: Assessment
) -> list[AdvisorLead]:
    """Score ``assessment`` against every approved advisor and persist leads."""
    stmt = (
        select(User, AdvisorProfile)
        .join(AdvisorProfile, AdvisorProfile.user_id == User.id)
        .where(User.role == UserRole.advisor)
        .where(User.is_active.is_(True))
        .where(User.verification_status == VerificationStatus.approved)
    )
    rows = (await session.execute(stmt)).all()
    ratings = await review_service.rating_summaries(session, [user.id for user, _ in rows])

    leads: list[AdvisorLead] = []
    for user, profile in rows:
        average_rating = ratings[user.id][0] if user.id in ratings else None
        score = score_advisor_for_assessment(
            profile, assessment.destination_country, assessment.visa_type, average_rating
        )
        if score <= 0:
            continue
        lead = AdvisorLead(
            seeker_id=assessment.user_id,
            advisor_id=user.id,
            assessment_id=assessment.id,
            match_score=score,
            match_reasons=_build_reasons(
                profile, assessment.destination_country, assessment.visa_type, average_rating
            ),
            status=AdvisorLeadStatus.new,
        )
        session.add(lead)
        leads.append(lead)

    if leads:
        await session.flush()
        for lead in leads:
            await session.refresh(lead)
    return leads


def list_for_advisor_stmt(
    advisor_id: uuid.UUID, status: AdvisorLeadStatus | None = None
) -> Select[tuple[AdvisorLead]]:
    stmt = (
        select(AdvisorLead)
        .where(AdvisorLead.advisor_id == advisor_id)
        .order_by(AdvisorLead.match_score.desc(), AdvisorLead.created_at.desc())
    )
    if status is not None:
        stmt = stmt.where(AdvisorLead.status == status)
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
