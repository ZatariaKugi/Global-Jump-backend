"""Seeker home dashboard — composes visa journey state, document summary,
the latest completed assessment, and AI advisor matching into one payload.
Distinct from dashboard_service.py (admin home) and advisor_dashboard_service.py.

An optional ``days`` window scopes the completed-assessment lookup and the
documents-uploaded tile — everything else is current-state.
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
from app.schemas.assessment import AdvisorMatchRead
from app.schemas.booking import BookingRead
from app.schemas.seeker_dashboard import (
    EligibilityBreakdownRead,
    EligibilityBreakdownSegment,
    JourneyStageRead,
    SeekerDashboardRead,
    SeekerDashboardStats,
)
from app.schemas.visa_journey import JourneyStepKey, JourneyStepStatus
from app.services import advisor_matching_service, booking_service, visa_journey_service

MATCHED_ADVISORS_LIMIT = 8

# Chart labels differ from the visa-journey stepper copy: the hidden
# application_preparation step surfaces here as the "Review" bar.
_STAGE_LABELS: dict[JourneyStepKey, str] = {
    JourneyStepKey.assessment: "Assessment",
    JourneyStepKey.advisor: "Advisor",
    JourneyStepKey.documentation: "Documents",
    JourneyStepKey.application_preparation: "Review",
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
    visa_type: VisaType | None,
    country: str | None,
    since: datetime | None = None,
) -> Assessment | None:
    stmt = select(Assessment).where(
        Assessment.user_id == user_id,
        Assessment.status == AssessmentStatus.completed,
    )
    if visa_type is not None:
        stmt = stmt.where(Assessment.visa_type == visa_type.value)
    if country is not None:
        stmt = stmt.where(Assessment.destination_country == country.upper())
    if since is not None:
        stmt = stmt.where(Assessment.completed_at >= since)
    stmt = stmt.order_by(
        Assessment.completed_at.desc().nulls_last(), Assessment.created_at.desc()
    ).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def _documents_uploaded_since(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    visa_type: VisaType | None,
    since: datetime,
) -> int:
    stmt = select(func.count(SeekerDocument.id)).where(
        SeekerDocument.seeker_id == seeker_id,
        SeekerDocument.is_archived.is_(False),
        SeekerDocument.created_at >= since,
    )
    if visa_type is not None:
        # Untagged docs apply to every visa filter — parity with list_by_seeker_stmt.
        stmt = stmt.where(
            or_(
                SeekerDocument.visa_type == visa_type.value,
                SeekerDocument.visa_type.is_(None),
            )
        )
    return int((await session.execute(stmt)).scalar_one())


def _build_stages(
    statuses: dict[JourneyStepKey, JourneyStepStatus], doc_progress: int
) -> list[JourneyStageRead]:
    keys = visa_journey_service.STEP_KEYS
    active_key = next(
        (k for k in keys if statuses[k] == JourneyStepStatus.in_progress),
        next((k for k in keys if statuses[k] == JourneyStepStatus.pending), keys[-1]),
    )
    stages = []
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
                progress_percent=percent,
                active=key == active_key,
            )
        )
    return stages


async def get_dashboard(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    settings: Settings,
    *,
    visa_type: VisaType | None,
    country: str | None,
    days: int | None = None,
) -> SeekerDashboardRead:
    """Build the seeker home dashboard for the resolved visa/country scope."""
    since = datetime.now(UTC) - timedelta(days=days) if days is not None else None
    next_booking = await booking_service.get_next_upcoming(session, seeker_id, UserRole.seeker)
    next_upcoming: BookingRead | None = None
    if next_booking is not None:
        next_upcoming = await booking_service.read_booking(
            session, next_booking, settings=settings, viewer_role=UserRole.seeker
        )

    state = await visa_journey_service.compute_state(
        session, seeker_id, visa_type=visa_type, country=country
    )
    journey_progress = visa_journey_service.overall_progress(
        state.statuses, state.summary.progress_percent
    )
    total_steps = len(visa_journey_service.VISIBLE_STEP_KEYS)
    completed_steps = sum(
        1
        for key in visa_journey_service.VISIBLE_STEP_KEYS
        if state.statuses[key] == JourneyStepStatus.completed
    )
    application_status = int(round(100 * completed_steps / total_steps)) if total_steps else 0

    assessment = await _latest_completed_assessment(
        session, seeker_id, visa_type, country, since=since
    )

    matched: list[AdvisorMatchRead] = []
    if assessment is not None:
        matched, _total = await advisor_matching_service.match(
            session, assessment, limit=MATCHED_ADVISORS_LIMIT
        )

    breakdown: EligibilityBreakdownRead | None = None
    score = assessment.score if assessment is not None else None
    if score is not None:
        eligibility = max(0, min(int(round(score)), 100))
        breakdown = EligibilityBreakdownRead(
            center_percent=journey_progress,
            segments=[
                EligibilityBreakdownSegment(
                    key="eligibility", label="Eligibility", value=eligibility
                ),
                EligibilityBreakdownSegment(
                    key="missing_requirements",
                    label="Missing Requirements",
                    value=100 - eligibility,
                ),
            ],
        )

    source = assessment or state.assessment
    resolved_visa = visa_type or (parse_visa_type(source.visa_type) if source else None)
    resolved_country = country or (source.destination_country if source else None)

    documents_uploaded = state.summary.total
    if since is not None:
        documents_uploaded = await _documents_uploaded_since(session, seeker_id, visa_type, since)

    return SeekerDashboardRead(
        window_days=days,
        next_upcoming=next_upcoming,
        stats=SeekerDashboardStats(
            eligibility_score=score,
            eligibility_tier=assessment.tier if assessment is not None else None,
            visa_type=resolved_visa,
            visa_type_name=visa_type_name(resolved_visa),
            country=resolved_country,
            country_name=country_name(resolved_country),
            journey_progress_percent=journey_progress,
            documents_uploaded=documents_uploaded,
            documents_progress_percent=state.summary.progress_percent,
            application_status_percent=application_status,
        ),
        journey_stages=_build_stages(state.statuses, state.summary.progress_percent),
        eligibility_breakdown=breakdown,
        matched_advisors=matched,
        assessment_id=assessment.id if assessment is not None else None,
    )
