"""Seeker home dashboard — composes visa journey state, document summary,
the latest completed assessment, and profile-based advisor recommendations.
Distinct from dashboard_service.py (admin home) and advisor_dashboard_service.py.

Omitted ``visa_type`` is all visa types (same as GET /users/me/documents).
An explicit ``visa_type`` scopes stats, journey, eligibility,
``documents_uploaded``, and profile ``matched_advisors`` to that visa
(untagged docs included). ``days`` is independent (omit = all-time) and
combines with visa when both are set. Missing ``visa_type`` is never filled
from the profile or latest assessment. Eligibility uses the latest completed
assessment in that scope (score 0 if none in the ``days`` window).
``matched_advisors`` is current-state from ``seeker_advisor_recommendations``,
not AI Assessment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.countries import country_name
from app.core.visa_types import parse_visa_type, visa_type_name
from app.models.assessment import Assessment, AssessmentStatus
from app.models.seeker_document import SeekerDocument
from app.models.user import UserRole
from app.models.visa_type import VisaType
from app.schemas.booking import BookingRead
from app.schemas.seeker_dashboard import (
    EligibilityBreakdownRead,
    EligibilityBreakdownSegment,
    JourneyStageRead,
    SeekerDashboardRead,
    SeekerDashboardStats,
)
from app.schemas.visa_journey import JourneyStepKey, JourneyStepStatus
from app.services import (
    booking_service,
    seeker_profile_service,
    seeker_recommendation_service,
    visa_journey_service,
)

MATCHED_ADVISORS_LIMIT = 10

# Chart labels for the four seeker-facing timeline bars (Review is not shown).
_STAGE_LABELS: dict[JourneyStepKey, str] = {
    JourneyStepKey.assessment: "Assessment",
    JourneyStepKey.advisor: "Advisor",
    JourneyStepKey.documentation: "Documents",
    JourneyStepKey.submission: "Submission",
}

_STATUS_PROGRESS: dict[JourneyStepStatus, int] = {
    JourneyStepStatus.completed: 100,
    JourneyStepStatus.in_progress: 50,
    JourneyStepStatus.pending: 0,
}


async def _latest_completed_assessment(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    since: datetime | None = None,
    visa_type: VisaType | None = None,
    country: str | None = None,
) -> Assessment | None:
    """Latest completed assessment, optionally scoped by window / visa / country."""
    stmt = select(Assessment).where(
        Assessment.user_id == user_id,
        Assessment.status == AssessmentStatus.completed,
    )
    if since is not None:
        stmt = stmt.where(Assessment.completed_at >= since)
    if visa_type is not None:
        stmt = stmt.where(Assessment.visa_type == visa_type.value)
    if country is not None:
        stmt = stmt.where(Assessment.destination_country == country.upper())
    stmt = stmt.order_by(
        Assessment.completed_at.desc().nulls_last(),
        Assessment.created_at.desc(),
    ).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def _documents_uploaded(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    since: datetime | None = None,
    visa_type: VisaType | None = None,
) -> int:
    """Active document count. No ``visa_type`` = all visas; otherwise that visa
    plus untagged docs (same as GET /users/me/documents). ``since`` is the
    optional ``days`` window."""
    stmt = select(func.count(SeekerDocument.id)).where(
        SeekerDocument.seeker_id == seeker_id,
        SeekerDocument.is_archived.is_(False),
    )
    if visa_type is not None:
        stmt = stmt.where(
            or_(
                SeekerDocument.visa_type == visa_type.value,
                SeekerDocument.visa_type.is_(None),
            )
        )
    if since is not None:
        stmt = stmt.where(SeekerDocument.created_at >= since)
    return int((await session.execute(stmt)).scalar_one())


def _build_stages(
    statuses: dict[JourneyStepKey, JourneyStepStatus], doc_progress: int
) -> list[JourneyStageRead]:
    """Dashboard timeline bars — Assessment → Advisor → Documents → Submission."""
    keys = tuple(
        key
        for key in visa_journey_service.VISIBLE_STEP_KEYS
        if key is not JourneyStepKey.application_preparation
    )
    active_key = next(
        (k for k in keys if statuses[k] == JourneyStepStatus.in_progress),
        next((k for k in keys if statuses[k] == JourneyStepStatus.pending), keys[-1]),
    )
    stages: list[JourneyStageRead] = []
    for key in keys:
        status = statuses[key]
        if key == JourneyStepKey.documentation and status == JourneyStepStatus.in_progress:
            percent = max(0, min(doc_progress, 100))
        else:
            percent = _STATUS_PROGRESS[status]
        stages.append(
            JourneyStageRead(
                key=key,
                label=_STAGE_LABELS[key],
                status=status,
                progress_percent=percent,
                active=key == active_key,
            )
        )
    return stages


async def resolve_scope(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    visa_type: VisaType | None,
    country: str | None,
) -> tuple[VisaType | None, str | None]:
    """Visa-journey scope: query override → latest completed assessment → profile.

    Dashboard does **not** use this — omitted ``visa_type`` there means all visas.
    """
    explicit_visa = visa_type is not None
    if visa_type is not None and country is not None:
        return visa_type, country
    latest = await _latest_completed_assessment(session, seeker_id)
    profile = await seeker_profile_service.get_or_create(session, seeker_id)
    if visa_type is None:
        visa_type = (
            parse_visa_type(latest.visa_type)
            if latest is not None
            else parse_visa_type(profile.intended_visa_type)
        )
    if country is None:
        scoped = latest
        if explicit_visa and visa_type is not None:
            scoped = await _latest_completed_assessment(session, seeker_id, visa_type=visa_type)
        if scoped is not None:
            country = scoped.destination_country
        elif profile.intended_destination:
            country = profile.intended_destination.upper()
    return visa_type, country


async def get_dashboard(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    settings: Settings,
    *,
    visa_type: VisaType | None,
    country: str | None,
    days: int | None = None,
) -> SeekerDashboardRead:
    """Build the seeker home dashboard.

    ``visa_type`` / ``country`` / ``days`` are query filters only — omitted
    ``visa_type`` means all visas, omitted ``days`` means all-time. They are
    never defaulted from the profile or latest assessment.
    """
    since = datetime.now(UTC) - timedelta(days=days) if days is not None else None
    next_booking = await booking_service.get_next_upcoming(session, seeker_id, UserRole.seeker)
    next_upcoming: BookingRead | None = None
    if next_booking is not None:
        next_upcoming = await booking_service.read_booking(
            session, next_booking, settings=settings, viewer_role=UserRole.seeker
        )

    assessment = await _latest_completed_assessment(
        session, seeker_id, since=since, visa_type=visa_type, country=country
    )

    state = await visa_journey_service.compute_state(
        session, seeker_id, visa_type=visa_type, country=country
    )
    doc_progress = visa_journey_service.documentation_progress(state.summary)
    journey_progress = visa_journey_service.overall_progress(state.statuses, doc_progress)
    application_percent = visa_journey_service.application_status_percent(
        state.statuses, doc_progress
    )
    application_state = visa_journey_service.application_status_state(state.statuses)

    matched = await seeker_recommendation_service.matches_for_dashboard(
        session,
        seeker_id,
        settings,
        visa_type=visa_type,
        country=country,
        limit=MATCHED_ADVISORS_LIMIT,
    )

    raw_score = assessment.score if assessment is not None else 0.0
    score = 0.0 if raw_score is None else float(raw_score)
    eligibility = max(0, min(int(round(score)), 100))
    breakdown = EligibilityBreakdownRead(
        center_percent=journey_progress,
        segments=[
            EligibilityBreakdownSegment(key="eligibility", label="Eligibility", value=eligibility),
            EligibilityBreakdownSegment(
                key="missing_requirements",
                label="Missing Requirements",
                value=100 - eligibility,
            ),
        ],
    )

    documents_uploaded = await _documents_uploaded(
        session, seeker_id, since=since, visa_type=visa_type
    )

    return SeekerDashboardRead(
        window_days=days,
        next_upcoming=next_upcoming,
        stats=SeekerDashboardStats(
            eligibility_score=score,
            eligibility_tier=assessment.tier if assessment is not None else None,
            visa_type=visa_type,
            visa_type_name=visa_type_name(visa_type),
            country=country,
            country_name=country_name(country),
            journey_progress_percent=journey_progress,
            documents_uploaded=documents_uploaded,
            documents_progress_percent=state.summary.progress_percent,
            application_status=application_state,
            application_status_percent=application_percent,
        ),
        journey_stages=_build_stages(state.statuses, doc_progress),
        eligibility_breakdown=breakdown,
        matched_advisors=matched,
        assessment_id=assessment.id if assessment is not None else None,
    )
