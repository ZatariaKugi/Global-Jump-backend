"""Seed assessment + match traffic for the admin AI Analytics panel.

Populates ``GET /api/v1/admin/analytics/ai`` with:
  - assessment_distribution (7 visa-type donut slices + change_pct)
  - advisor_match_funnel (impressions → matches → clicks → booked)
  - eligibility_breakdown (stacked low/medium/high % per visa type)

Creates a dedicated CA/student questionnaire (if missing) for abandoned
assessments, plus a seed advisor so funnel bookings can attach::

    uv run python -m scripts.seed_ai_analytics

Idempotent: deletes prior assessments/leads/bookings for the seed seeker
and recreates them. Covers the current and previous ~9-month windows so
``change_pct`` is non-zero.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.core.logging import get_logger
from app.core.security import hash_password
from app.core.visa_types import VISA_TYPE_LABELS
from app.db.session import async_session_factory, engine
from app.models.advisor_lead import AdvisorLead, AdvisorLeadStatus
from app.models.advisor_profile import AdvisorProfile
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentQuestion,
    AssessmentQuestionOption,
    AssessmentStatus,
    EligibilityTier,
    QuestionCategory,
)
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.user import User, UserRole, VerificationStatus
from app.models.visa_type import VisaType
from app.services import booking_service
from app.services.assessment_service import list_questions

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
COUNTRY = "CA"
SEEKER_EMAIL = "ai.analytics.seeker@globlejump.test"
ADVISOR_EMAIL = "ai.analytics.advisor@globlejump.test"

# Stable order matching the AI analytics API / FE donut.
VISA_TYPES: tuple[VisaType, ...] = (
    VisaType.student,
    VisaType.work,
    VisaType.tourist,
    VisaType.pr,
    VisaType.family,
    VisaType.investment,
    VisaType.asylum,
)

# Relative weights for the donut (current window).
VISA_WEIGHTS: dict[VisaType, float] = {
    VisaType.student: 0.28,
    VisaType.work: 0.22,
    VisaType.tourist: 0.14,
    VisaType.pr: 0.12,
    VisaType.family: 0.10,
    VisaType.investment: 0.08,
    VisaType.asylum: 0.06,
}

# Current window volumes by months-ago (sum ≈ distribution value).
CURRENT_MONTHLY: list[tuple[int, int]] = [
    (2, 40),
    (1, 48),
    (0, 52),
]

# Previous window (for change_pct) — outside the default 270-day lookback.
PREV_MONTHLY: list[tuple[int, int]] = [
    (12, 28),
    (11, 30),
    (10, 32),
]

# Of completed: high / medium / low tier mix.
HIGH_SHARE = 0.35
MEDIUM_SHARE = 0.40
# remainder → low

ABANDON_SHARE = 0.18

ANALYTICS_QUESTIONS: list[tuple[QuestionCategory, str]] = [
    (QuestionCategory.purpose, "What is your primary reason for immigrating?"),
    (QuestionCategory.education, "What is your highest education level?"),
    (QuestionCategory.employment, "How many years of relevant work experience do you have?"),
    (QuestionCategory.financial, "Can you show proof of funds for the first year?"),
]


async def _ensure_seeker(session) -> User:
    user = await session.scalar(select(User).where(User.email == SEEKER_EMAIL))
    if user is not None:
        return user
    user = User(
        email=SEEKER_EMAIL,
        full_name="AI Analytics Seed Seeker",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    logger.info("ai_analytics_seeker_created", email=SEEKER_EMAIL)
    return user


async def _ensure_advisor(session) -> User:
    user = await session.scalar(select(User).where(User.email == ADVISOR_EMAIL))
    if user is not None:
        return user
    user = User(
        email=ADVISOR_EMAIL,
        full_name="AI Analytics Seed Advisor",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.advisor,
        is_active=True,
        verification_status=VerificationStatus.approved,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    session.add(
        AdvisorProfile(
            user_id=user.id,
            title="Immigration Consultant",
            years_of_experience=8,
            bio="Temporary seed profile for admin AI analytics.",
        )
    )
    await session.flush()
    logger.info("ai_analytics_advisor_created", email=ADVISOR_EMAIL)
    return user


async def _ensure_questions(session) -> int:
    """Ensure ≥4 active questions for CA/student (creates scoped ones if needed)."""
    existing = await list_questions(session, COUNTRY, VisaType.student.value)
    if len(existing) >= len(ANALYTICS_QUESTIONS):
        return 0

    created = 0
    start_order = (
        await session.scalar(
            select(func.coalesce(func.max(AssessmentQuestion.display_order), -1))
        )
        or -1
    ) + 1
    for i, (category, text) in enumerate(ANALYTICS_QUESTIONS):
        dup = await session.scalar(
            select(AssessmentQuestion.id).where(
                AssessmentQuestion.text == text,
                AssessmentQuestion.country_code == COUNTRY,
                AssessmentQuestion.visa_type == VisaType.student.value,
            )
        )
        if dup is not None:
            continue
        session.add(
            AssessmentQuestion(
                text=text,
                category=category,
                country_code=COUNTRY,
                visa_type=VisaType.student.value,
                weight=1.0,
                display_order=start_order + i,
                is_active=True,
                options=[
                    AssessmentQuestionOption(
                        text="Yes / strong",
                        score=100,
                        display_order=0,
                    ),
                    AssessmentQuestionOption(
                        text="Somewhat",
                        score=50,
                        display_order=1,
                    ),
                    AssessmentQuestionOption(
                        text="No / weak",
                        score=10,
                        display_order=2,
                    ),
                ],
            )
        )
        created += 1
    if created:
        await session.flush()
        logger.info("ai_analytics_questions_created", count=created)
    return created


async def _clear_prior(session, seeker_id: uuid.UUID) -> tuple[int, int, int]:
    assessment_ids = list(
        (
            await session.execute(
                select(Assessment.id).where(Assessment.user_id == seeker_id)
            )
        )
        .scalars()
        .all()
    )
    leads_deleted = 0
    if assessment_ids:
        leads_deleted = (
            await session.execute(
                delete(AdvisorLead).where(AdvisorLead.assessment_id.in_(assessment_ids))
            )
        ).rowcount or 0
        await session.execute(
            delete(AssessmentAnswer).where(AssessmentAnswer.assessment_id.in_(assessment_ids))
        )
        await session.execute(delete(Assessment).where(Assessment.id.in_(assessment_ids)))

    bookings_deleted = (
        await session.execute(delete(Booking).where(Booking.seeker_id == seeker_id))
    ).rowcount or 0
    await session.flush()
    return len(assessment_ids), leads_deleted, bookings_deleted


def _month_anchor(months_ago: int) -> datetime:
    now = datetime.now(UTC)
    year = now.year
    month = now.month - months_ago
    while month <= 0:
        month += 12
        year -= 1
    day = min(15, now.day)
    return datetime(year, month, day, 12, 0, 0, tzinfo=UTC)


def _split_visa_volumes(total: int) -> dict[VisaType, int]:
    raw = {vt: int(total * VISA_WEIGHTS[vt]) for vt in VISA_TYPES}
    drift = total - sum(raw.values())
    raw[VisaType.student] += drift
    return raw


def _tier_counts(completed: int) -> tuple[int, int, int]:
    high = round(completed * HIGH_SHARE)
    medium = round(completed * MEDIUM_SHARE)
    low = max(0, completed - high - medium)
    return high, medium, low


async def _add_completed(
    session,
    *,
    seeker_id: uuid.UUID,
    visa: VisaType,
    created_at: datetime,
    tier: EligibilityTier,
    score: float,
) -> Assessment:
    assessment = Assessment(
        user_id=seeker_id,
        destination_country=COUNTRY,
        visa_type=visa.value,
        status=AssessmentStatus.completed,
        score=score,
        tier=tier,
        confidence=0.85,
        completed_at=created_at + timedelta(minutes=12),
        created_by=seeker_id,
    )
    assessment.created_at = created_at
    session.add(assessment)
    await session.flush()
    return assessment


async def _add_abandoned(
    session,
    *,
    seeker_id: uuid.UUID,
    visa: VisaType,
    created_at: datetime,
    questions: list[AssessmentQuestion],
) -> None:
    assessment = Assessment(
        user_id=seeker_id,
        destination_country=COUNTRY,
        visa_type=visa.value,
        status=AssessmentStatus.in_progress,
        created_by=seeker_id,
    )
    assessment.created_at = created_at
    session.add(assessment)
    await session.flush()
    if questions and questions[0].options:
        session.add(
            AssessmentAnswer(
                assessment_id=assessment.id,
                question_id=questions[0].id,
                option_id=questions[0].options[0].id,
            )
        )


async def _seed_window(
    session,
    *,
    seeker_id: uuid.UUID,
    advisor_id: uuid.UUID,
    monthly: list[tuple[int, int]],
    questions: list[AssessmentQuestion],
    make_funnel: bool,
) -> tuple[int, int, int, int]:
    """Returns (started, completed, leads, bookings)."""
    started = 0
    completed = 0
    leads = 0
    bookings = 0
    hour = 0

    for months_ago, volume in monthly:
        anchor = _month_anchor(months_ago)
        by_visa = _split_visa_volumes(volume)
        for visa, n in by_visa.items():
            abandon = max(0, round(n * ABANDON_SHARE))
            done = n - abandon
            high_n, med_n, low_n = _tier_counts(done)
            started += n
            completed += done

            tiers = (
                [(EligibilityTier.highly_eligible, 90.0)] * high_n
                + [(EligibilityTier.likely_eligible, 72.0)] * med_n
                + [(EligibilityTier.borderline, 45.0)] * (low_n // 2)
                + [(EligibilityTier.low_eligibility, 25.0)] * (low_n - low_n // 2)
            )
            for i, (tier, score) in enumerate(tiers):
                assessment = await _add_completed(
                    session,
                    seeker_id=seeker_id,
                    visa=visa,
                    created_at=anchor + timedelta(hours=hour + i),
                    tier=tier,
                    score=score,
                )
                if make_funnel and i % 2 == 0:
                    session.add(
                        AdvisorLead(
                            seeker_id=seeker_id,
                            advisor_id=advisor_id,
                            assessment_id=assessment.id,
                            match_score=0.82,
                            match_reasons=f"Specializes in {VISA_TYPE_LABELS[visa]}",
                            status=AdvisorLeadStatus.new,
                            created_by=seeker_id,
                        )
                    )
                    leads += 1
                if make_funnel and i % 3 == 0:
                    start = anchor + timedelta(days=7, hours=i)
                    # Mix pending (clicked) vs confirmed/completed (session booked).
                    if i % 9 == 0:
                        status = BookingStatus.completed
                        payment = PaymentStatus.paid
                    elif i % 6 == 0:
                        status = BookingStatus.confirmed
                        payment = PaymentStatus.paid
                    else:
                        status = BookingStatus.pending
                        payment = PaymentStatus.unpaid
                    booking = Booking(
                        seeker_id=seeker_id,
                        advisor_id=advisor_id,
                        appointment_number=await booking_service._next_appointment_number(
                            session
                        ),
                        scheduled_start=start,
                        scheduled_end=start + timedelta(minutes=30),
                        duration_minutes=30,
                        service_type="immigration_specialist",
                        price_usd=49.0,
                        status=status,
                        payment_status=payment,
                        created_by=seeker_id,
                    )
                    booking.created_at = anchor + timedelta(hours=hour + i, minutes=30)
                    session.add(booking)
                    bookings += 1

            hour += done
            for j in range(abandon):
                await _add_abandoned(
                    session,
                    seeker_id=seeker_id,
                    visa=visa,
                    created_at=anchor + timedelta(hours=hour + j),
                    questions=questions,
                )
            hour += abandon

    return started, completed, leads, bookings


async def seed_ai_analytics() -> list[str]:
    lines: list[str] = []

    async with async_session_factory() as session:
        seeker = await _ensure_seeker(session)
        advisor = await _ensure_advisor(session)
        cleared_a, cleared_l, cleared_b = await _clear_prior(session, seeker.id)
        lines.append(
            f"cleared assessments={cleared_a} leads={cleared_l} bookings={cleared_b}"
        )

        q_created = await _ensure_questions(session)
        lines.append(f"questions_created={q_created}")
        questions = await list_questions(session, COUNTRY, VisaType.student.value)
        lines.append(f"questions_for_scope={len(questions)}")

        prev_s, prev_c, prev_l, prev_b = await _seed_window(
            session,
            seeker_id=seeker.id,
            advisor_id=advisor.id,
            monthly=PREV_MONTHLY,
            questions=questions,
            make_funnel=True,
        )
        cur_s, cur_c, cur_l, cur_b = await _seed_window(
            session,
            seeker_id=seeker.id,
            advisor_id=advisor.id,
            monthly=CURRENT_MONTHLY,
            questions=questions,
            make_funnel=True,
        )
        await session.commit()

        lines.append(f"prev_window started={prev_s} completed={prev_c} leads={prev_l} bookings={prev_b}")
        lines.append(f"curr_window started={cur_s} completed={cur_c} leads={cur_l} bookings={cur_b}")
        lines.append(f"visa_types={','.join(vt.value for vt in VISA_TYPES)}")
        lines.append(f"seeker={SEEKER_EMAIL}")
        lines.append(f"advisor={ADVISOR_EMAIL}")
    return lines


async def main() -> None:
    try:
        for line in await seed_ai_analytics():
            print(line)
        print()
        print("AI Analytics: GET /api/v1/admin/analytics/ai")
        print("  default window: ?days=270")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
