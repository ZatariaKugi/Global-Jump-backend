"""Seed assessment traffic for the admin AI Analytics panel.

Populates ``GET /api/v1/admin/analytics/ai`` with:
  - pass_rate / fail_rate (completed assessments by tier)
  - assessment_volume (started counts by month)
  - drop_off_points (abandoned at Q1…Qn as % of started)

Creates a dedicated CA/student questionnaire (if missing) so drop-off stages
work even when the global question bank is empty or differently scoped::

    uv run python -m scripts.seed_ai_analytics

Idempotent: deletes prior assessments for the seed seeker and recreates them.
Covers 8 calendar months. The AI analytics endpoint defaults to ``days=270``
so the volume chart shows the full series without an extra query param.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentQuestion,
    AssessmentQuestionOption,
    AssessmentStatus,
    EligibilityTier,
    QuestionCategory,
)
from app.models.user import User, UserRole
from app.services.assessment_service import list_questions

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
COUNTRY = "CA"
VISA = "student"
SEEKER_EMAIL = "ai.analytics.seeker@globlejump.test"

# Minimal questionnaire used only for analytics drop-off staging (Q1–Q4).
ANALYTICS_QUESTIONS: list[tuple[QuestionCategory, str]] = [
    (QuestionCategory.purpose, "What is your primary reason for immigrating?"),
    (QuestionCategory.education, "What is your highest education level?"),
    (QuestionCategory.employment, "How many years of relevant work experience do you have?"),
    (QuestionCategory.financial, "Can you show proof of funds for the first year?"),
]

# Months ago → started count (8 months of volume for the area chart).
MONTHLY_VOLUME: list[tuple[int, int]] = [
    (7, 22),
    (6, 26),
    (5, 28),
    (4, 34),
    (3, 40),
    (2, 36),
    (1, 44),
    (0, 48),
]

# Of completed assessments: ~78% pass / ~22% fail (matches FE donut examples).
PASS_SHARE = 0.78

# Of started: ~22% abandon; remainder complete.
ABANDON_SHARE = 0.22

# Abandoned assessments distributed across early stages (Q1–Q4).
DROP_STAGE_WEIGHTS: list[tuple[int, float]] = [
    (1, 0.35),  # Q1
    (2, 0.28),  # Q2
    (3, 0.22),  # Q3
    (4, 0.15),  # Q4
]

PASS_TIERS = (
    EligibilityTier.highly_eligible,
    EligibilityTier.likely_eligible,
)
FAIL_TIERS = (
    EligibilityTier.borderline,
    EligibilityTier.low_eligibility,
)


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


async def _ensure_questions(session) -> int:
    """Ensure ≥4 active questions for CA/student (creates scoped ones if needed)."""
    existing = await list_questions(session, COUNTRY, VISA)
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
        # Skip if an identical scoped question already exists.
        dup = await session.scalar(
            select(AssessmentQuestion.id).where(
                AssessmentQuestion.text == text,
                AssessmentQuestion.country_code == COUNTRY,
                AssessmentQuestion.visa_type == VISA,
            )
        )
        if dup is not None:
            continue
        session.add(
            AssessmentQuestion(
                text=text,
                category=category,
                country_code=COUNTRY,
                visa_type=VISA,
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


async def _clear_prior(session, seeker_id: uuid.UUID) -> int:
    ids = list(
        (
            await session.execute(
                select(Assessment.id).where(Assessment.user_id == seeker_id)
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return 0
    await session.execute(delete(AssessmentAnswer).where(AssessmentAnswer.assessment_id.in_(ids)))
    await session.execute(delete(Assessment).where(Assessment.id.in_(ids)))
    await session.flush()
    return len(ids)


def _month_anchor(months_ago: int) -> datetime:
    """Mid-month UTC timestamp ``months_ago`` calendar months back from now."""
    now = datetime.now(UTC)
    year = now.year
    month = now.month - months_ago
    while month <= 0:
        month += 12
        year -= 1
    day = min(15, now.day)
    return datetime(year, month, day, 12, 0, 0, tzinfo=UTC)


def _split_counts(total: int) -> tuple[int, int, dict[int, int]]:
    """Return (pass_completed, fail_completed, {stage: abandon_count})."""
    abandon = max(1, round(total * ABANDON_SHARE)) if total >= 5 else max(0, total // 5)
    completed = total - abandon
    pass_n = round(completed * PASS_SHARE)
    fail_n = completed - pass_n

    stage_counts: dict[int, int] = {}
    remaining = abandon
    for i, (stage, weight) in enumerate(DROP_STAGE_WEIGHTS):
        if i == len(DROP_STAGE_WEIGHTS) - 1:
            stage_counts[stage] = remaining
        else:
            n = round(abandon * weight)
            stage_counts[stage] = n
            remaining -= n
    # Fix rounding drift so stages sum to abandon.
    drift = abandon - sum(stage_counts.values())
    if drift and stage_counts:
        first = next(iter(stage_counts))
        stage_counts[first] = max(0, stage_counts[first] + drift)
    return pass_n, fail_n, stage_counts


async def _add_completed(
    session,
    *,
    seeker_id: uuid.UUID,
    created_at: datetime,
    tier: EligibilityTier,
    score: float,
) -> None:
    assessment = Assessment(
        user_id=seeker_id,
        destination_country=COUNTRY,
        visa_type=VISA,
        status=AssessmentStatus.completed,
        score=score,
        tier=tier,
        confidence=0.85,
        completed_at=created_at + timedelta(minutes=12),
        created_by=seeker_id,
    )
    assessment.created_at = created_at
    session.add(assessment)


async def _add_abandoned(
    session,
    *,
    seeker_id: uuid.UUID,
    created_at: datetime,
    questions: list[AssessmentQuestion],
    drop_at_stage: int,
) -> None:
    """Leave unanswered from ``drop_at_stage`` onward (1-based Q index)."""
    assessment = Assessment(
        user_id=seeker_id,
        destination_country=COUNTRY,
        visa_type=VISA,
        status=AssessmentStatus.in_progress,
        created_by=seeker_id,
    )
    assessment.created_at = created_at
    session.add(assessment)
    await session.flush()

    answered_through = max(0, drop_at_stage - 1)
    for q in questions[:answered_through]:
        if not q.options:
            continue
        session.add(
            AssessmentAnswer(
                assessment_id=assessment.id,
                question_id=q.id,
                option_id=q.options[0].id,
            )
        )


async def seed_ai_analytics() -> list[str]:
    lines: list[str] = []

    async with async_session_factory() as session:
        seeker = await _ensure_seeker(session)
        cleared = await _clear_prior(session, seeker.id)
        lines.append(f"cleared_prior_assessments={cleared}")

        q_created = await _ensure_questions(session)
        lines.append(f"questions_created={q_created}")

        questions = await list_questions(session, COUNTRY, VISA)
        if not questions:
            lines.append("error=no_questions_configured")
            await session.commit()
            return lines
        lines.append(f"questions_for_scope={len(questions)}")

        total_started = 0
        total_pass = 0
        total_fail = 0
        total_abandon = 0

        for months_ago, volume in MONTHLY_VOLUME:
            anchor = _month_anchor(months_ago)
            pass_n, fail_n, stage_counts = _split_counts(volume)
            total_started += volume
            total_pass += pass_n
            total_fail += fail_n
            total_abandon += sum(stage_counts.values())

            for i in range(pass_n):
                tier = PASS_TIERS[i % len(PASS_TIERS)]
                score = 88.0 if tier == EligibilityTier.highly_eligible else 72.0
                await _add_completed(
                    session,
                    seeker_id=seeker.id,
                    created_at=anchor + timedelta(hours=i),
                    tier=tier,
                    score=score,
                )
            for i in range(fail_n):
                tier = FAIL_TIERS[i % len(FAIL_TIERS)]
                score = 45.0 if tier == EligibilityTier.borderline else 25.0
                await _add_completed(
                    session,
                    seeker_id=seeker.id,
                    created_at=anchor + timedelta(hours=pass_n + i),
                    tier=tier,
                    score=score,
                )
            offset = pass_n + fail_n
            for stage, count in stage_counts.items():
                for j in range(count):
                    await _add_abandoned(
                        session,
                        seeker_id=seeker.id,
                        created_at=anchor + timedelta(hours=offset + j),
                        questions=questions,
                        drop_at_stage=stage,
                    )
                offset += count

        await session.commit()
        completed = total_pass + total_fail
        pass_rate = round(100 * total_pass / completed, 1) if completed else 0.0
        fail_rate = round(100 * total_fail / completed, 1) if completed else 0.0
        lines.append(f"started={total_started}")
        lines.append(f"completed={completed} pass={total_pass} fail={total_fail}")
        lines.append(f"abandoned={total_abandon}")
        lines.append(f"approx_pass_rate={pass_rate} fail_rate={fail_rate}")
        lines.append(f"months={len(MONTHLY_VOLUME)}")
        lines.append(f"seeker={SEEKER_EMAIL}")
    return lines


async def main() -> None:
    try:
        for line in await seed_ai_analytics():
            print(line)
        print()
        print("AI Analytics: GET /api/v1/admin/analytics/ai")
        print("  default window: ?days=270 (8 months of volume)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
