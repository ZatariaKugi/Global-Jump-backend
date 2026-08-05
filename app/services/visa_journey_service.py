"""Seeker Visa Journey Tracking — derived from assessment, bookings, and documents.

No separate journey table: step status is computed from existing portfolio data
for the requested (or profile-default) visa type + destination country.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.countries import country_name
from app.core.file_storage import resolve_media_url
from app.core.visa_types import parse_visa_type, visa_type_name
from app.models.assessment import Assessment, AssessmentStatus
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.booking_document_request import BookingDocumentRequest, DocumentRequestStatus
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.models.visa_type import VisaType
from app.schemas.seeker_document import DocumentPortfolioSummary
from app.schemas.visa_journey import (
    AdvisorSuggestion,
    JourneyStepKey,
    JourneyStepStatus,
    VisaJourneyRead,
    VisaJourneyStep,
)
from app.services import booking_service, conversation_service, seeker_document_service
from app.services.conversation_service import PUBLIC_STATUSES

_STEP_COPY: tuple[tuple[JourneyStepKey, str, str], ...] = (
    (
        JourneyStepKey.assessment,
        "Assessment",
        "We have reviewed your assessment",
    ),
    (
        JourneyStepKey.advisor,
        "Advisor",
        "You have selected your advisor and consultation completed",
    ),
    (
        JourneyStepKey.documentation,
        "Documentation",
        "Please upload and verify all the documents for application",
    ),
    (
        JourneyStepKey.application_preparation,
        "Application preparation",
        "We have prepared an application and will share with you",
    ),
    (
        JourneyStepKey.submission,
        "Submission",
        "Submit your application and track the decision",
    ),
)

_ACTIVE_BOOKING = (
    BookingStatus.pending,
    BookingStatus.confirmed,
    BookingStatus.completed,
)

STEP_KEYS: tuple[JourneyStepKey, ...] = tuple(key for key, _, _ in _STEP_COPY)

# application_preparation is computed but hidden from the visa-journey stepper;
# progress/completed counts are defined over the visible steps only.
VISIBLE_STEP_KEYS: tuple[JourneyStepKey, ...] = tuple(
    key for key in STEP_KEYS if key != JourneyStepKey.application_preparation
)


@dataclass
class JourneyState:
    """Computed journey inputs shared by the visa-journey endpoint and the
    seeker dashboard aggregate — statuses cover all ``STEP_KEYS`` including
    the hidden ``application_preparation``."""

    assessment: Assessment | None
    booking: Booking | None
    summary: DocumentPortfolioSummary
    statuses: dict[JourneyStepKey, JourneyStepStatus]


async def _latest_assessment(
    session: AsyncSession,
    user_id: uuid.UUID,
    visa_type: VisaType | None,
    country: str | None,
) -> Assessment | None:
    stmt = select(Assessment).where(Assessment.user_id == user_id)
    if visa_type is not None:
        stmt = stmt.where(Assessment.visa_type == visa_type.value)
    if country is not None:
        stmt = stmt.where(Assessment.destination_country == country.upper())
    stmt = stmt.order_by(Assessment.created_at.desc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def _latest_booking(session: AsyncSession, seeker_id: uuid.UUID) -> Booking | None:
    stmt = (
        select(Booking)
        .where(Booking.seeker_id == seeker_id)
        .where(Booking.status.in_(_ACTIVE_BOOKING))
        .order_by(Booking.updated_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _open_document_requests(
    session: AsyncSession, booking_id: uuid.UUID
) -> list[BookingDocumentRequest]:
    result = await session.execute(
        select(BookingDocumentRequest)
        .where(BookingDocumentRequest.booking_id == booking_id)
        .where(BookingDocumentRequest.status == DocumentRequestStatus.requested)
        .where(BookingDocumentRequest.is_archived.is_(False))
        .order_by(BookingDocumentRequest.created_at.asc())
    )
    return list(result.scalars().all())


async def _all_requests_fulfilled(session: AsyncSession, booking_id: uuid.UUID) -> bool:
    result = await session.execute(
        select(BookingDocumentRequest)
        .where(BookingDocumentRequest.booking_id == booking_id)
        .where(BookingDocumentRequest.is_archived.is_(False))
    )
    rows = list(result.scalars().all())
    if not rows:
        return False
    return all(r.status == DocumentRequestStatus.fulfilled for r in rows)


async def _latest_advisor_message(
    session: AsyncSession, seeker_id: uuid.UUID, advisor_id: uuid.UUID
) -> Message | None:
    conversation = (
        (
            await session.execute(
                select(Conversation).where(
                    Conversation.seeker_id == seeker_id,
                    Conversation.advisor_id == advisor_id,
                    Conversation.is_archived.is_(False),
                )
            )
        )
        .scalars()
        .first()
    )
    if conversation is None:
        return None
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .where(Message.sender_id == advisor_id)
        .where(Message.moderation_status.in_(PUBLIC_STATUSES))
        .where(Message.deleted_at.is_(None))
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


def _assessment_status(assessment: Assessment | None) -> JourneyStepStatus:
    if assessment is None:
        return JourneyStepStatus.pending
    if assessment.status == AssessmentStatus.completed:
        return JourneyStepStatus.completed
    return JourneyStepStatus.in_progress


def _advisor_status(booking: Booking | None, unlocked: bool) -> JourneyStepStatus:
    if not unlocked:
        return JourneyStepStatus.pending
    if booking is None:
        return JourneyStepStatus.pending
    if booking.payment_status == PaymentStatus.paid:
        return JourneyStepStatus.completed
    return JourneyStepStatus.in_progress


def _documentation_status(
    unlocked: bool, *, summary: DocumentPortfolioSummary
) -> JourneyStepStatus:
    if not unlocked:
        return JourneyStepStatus.pending
    # Advisor rejected at least one document → send the seeker back to fix it.
    if summary.rejected > 0:
        return JourneyStepStatus.pending
    # Every required document uploaded and approved by the advisor.
    if summary.total > 0 and summary.approved == summary.total and summary.missing == 0:
        return JourneyStepStatus.completed
    return JourneyStepStatus.in_progress


def _app_prep_status(unlocked: bool, prep_done: bool) -> JourneyStepStatus:
    if not unlocked:
        return JourneyStepStatus.pending
    if prep_done:
        return JourneyStepStatus.completed
    return JourneyStepStatus.in_progress


def _submission_status(unlocked: bool, *, done: bool) -> JourneyStepStatus:
    if not unlocked:
        return JourneyStepStatus.pending
    if done:
        return JourneyStepStatus.completed
    return JourneyStepStatus.in_progress


def overall_progress(statuses: dict[JourneyStepKey, JourneyStepStatus], doc_progress: int) -> int:
    """Weighted 0–100 progress over the visible steps only."""
    total = len(VISIBLE_STEP_KEYS)
    if total == 0:
        return 0
    score = 0.0
    for key in VISIBLE_STEP_KEYS:
        status = statuses[key]
        if status == JourneyStepStatus.completed:
            score += 1.0
        elif status == JourneyStepStatus.in_progress:
            if key == JourneyStepKey.documentation:
                score += max(0.0, min(doc_progress, 100)) / 100.0
            else:
                score += 0.5
    return int(round(100 * score / total))


async def _build_suggestion(
    session: AsyncSession,
    settings: Settings,
    seeker_id: uuid.UUID,
    booking: Booking | None,
    missing_labels: list[str],
) -> AdvisorSuggestion | None:
    if booking is None:
        return None

    advisor = await session.get(User, booking.advisor_id)
    photos = await booking_service.advisor_photo_keys(session, {booking.advisor_id})
    photo_url = resolve_media_url(photos.get(booking.advisor_id), settings)
    advisor_name = advisor.full_name if advisor else None

    requests = await _open_document_requests(session, booking.id)
    if requests:
        names = [r.description.strip() for r in requests if r.description.strip()]
        listed = ", ".join(f"[{n}]" for n in names) if names else "the requested documents"
        body = (
            "I reviewed your application and noticed that some required documents "
            f"are missing. Please provide the following documents: {listed}."
        )
        latest = max((r.created_at for r in requests), default=None)
        return AdvisorSuggestion(
            advisor_id=booking.advisor_id,
            advisor_name=advisor_name,
            advisor_photo_url=photo_url,
            body=body,
            source="document_request",
            created_at=latest,
        )

    message = await _latest_advisor_message(session, seeker_id, booking.advisor_id)
    if message is not None:
        preview = conversation_service.message_preview(message)
        if preview:
            return AdvisorSuggestion(
                advisor_id=booking.advisor_id,
                advisor_name=advisor_name,
                advisor_photo_url=photo_url,
                body=preview,
                source="message",
                created_at=message.created_at,
            )

    if missing_labels:
        listed = ", ".join(f"[{n}]" for n in missing_labels)
        body = (
            "I reviewed your application and noticed that some required documents "
            f"are missing. Please provide the following documents: {listed}."
        )
        return AdvisorSuggestion(
            advisor_id=booking.advisor_id,
            advisor_name=advisor_name,
            advisor_photo_url=photo_url,
            body=body,
            source="checklist",
            created_at=None,
        )

    return None


async def compute_state(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    visa_type: VisaType | None,
    country: str | None,
) -> JourneyState:
    """Compute step statuses (all ``STEP_KEYS``) from portfolio data."""
    assessment = await _latest_assessment(session, seeker_id, visa_type, country)
    booking = await _latest_booking(session, seeker_id)
    summary = await seeker_document_service.portfolio_summary(
        session, seeker_id, visa_type=visa_type
    )

    assessment_st = _assessment_status(assessment)
    advisor_unlocked = assessment_st == JourneyStepStatus.completed
    advisor_st = _advisor_status(booking, advisor_unlocked)

    docs_unlocked = advisor_st in (
        JourneyStepStatus.completed,
        JourneyStepStatus.in_progress,
    )
    docs_st = _documentation_status(
        docs_unlocked,
        summary=summary,
    )

    prep_unlocked = docs_st == JourneyStepStatus.completed
    prep_done = False
    if prep_unlocked and booking is not None:
        prep_done = await _all_requests_fulfilled(session, booking.id)
    prep_st = _app_prep_status(prep_unlocked, prep_done)

    # Documentation completing (all docs advisor-approved) also completes submission.
    submission_unlocked = docs_st == JourneyStepStatus.completed
    submission_st = _submission_status(
        submission_unlocked, done=docs_st == JourneyStepStatus.completed
    )

    statuses = {
        JourneyStepKey.assessment: assessment_st,
        JourneyStepKey.advisor: advisor_st,
        JourneyStepKey.documentation: docs_st,
        JourneyStepKey.application_preparation: prep_st,
        JourneyStepKey.submission: submission_st,
    }
    return JourneyState(
        assessment=assessment,
        booking=booking,
        summary=summary,
        statuses=statuses,
    )


async def get_journey(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    settings: Settings,
    *,
    visa_type: VisaType | None,
    country: str | None,
) -> VisaJourneyRead:
    """Build the Visa Journey Tracking payload for the seeker."""
    state = await compute_state(session, seeker_id, visa_type=visa_type, country=country)
    assessment, booking, summary = state.assessment, state.booking, state.summary
    statuses = state.statuses

    visible_step_copy = tuple(item for item in _STEP_COPY if item[0] in VISIBLE_STEP_KEYS)

    steps: list[VisaJourneyStep] = []
    for order, (key, title, description) in enumerate(visible_step_copy, start=1):
        status = statuses[key]
        action: str | None = None
        action_label: str | None = None
        if key == JourneyStepKey.documentation and status == JourneyStepStatus.in_progress:
            action = "go_to_documents"
            action_label = "Go to documents"
        steps.append(
            VisaJourneyStep(
                key=key,
                title=title,
                description=description,
                status=status,
                order=order,
                action=action,
                action_label=action_label,
            )
        )

    completed_steps = sum(1 for s in steps if s.status == JourneyStepStatus.completed)
    current = next(
        (s for s in steps if s.status == JourneyStepStatus.in_progress),
        next((s for s in steps if s.status == JourneyStepStatus.pending), steps[-1]),
    )
    status_label = (
        "Completed"
        if current.status == JourneyStepStatus.completed
        else "In progress"
        if current.status == JourneyStepStatus.in_progress
        else "Pending"
    )

    missing_labels = [i.label for i in summary.checklist if i.status == "missing"]
    suggestion = await _build_suggestion(session, settings, seeker_id, booking, missing_labels)

    resolved_visa = visa_type or (parse_visa_type(assessment.visa_type) if assessment else None)
    resolved_country = (
        country.upper() if country else (assessment.destination_country if assessment else None)
    )

    return VisaJourneyRead(
        visa_type=resolved_visa,
        visa_type_name=visa_type_name(resolved_visa),
        country=resolved_country,
        country_name=country_name(resolved_country),
        current_status=current.title,
        current_status_label=status_label,
        completed_steps=completed_steps,
        total_steps=len(steps),
        progress_percent=overall_progress(statuses, summary.progress_percent),
        steps=steps,
        advisor_suggestion=suggestion,
        assessment_id=assessment.id if assessment else None,
        booking_id=booking.id if booking else None,
    )
