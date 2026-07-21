"""Eligibility assessment engine — questionnaire selection and weighted scoring.

Deterministic rule engine (PRD §3.4): each answered option contributes its score
(0–100) weighted by the question's weight. The overall score maps to a tier
(§3.4.2) and low-scoring answers surface their improvement tips.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentCategoryScore,
    AssessmentInsight,
    AssessmentQuestion,
    AssessmentQuestionOption,
    AssessmentStatus,
    AssessmentTip,
    EligibilityTier,
    InsightKind,
)
from app.models.assessment_threshold import AssessmentThreshold
from app.schemas.assessment import (
    AnswerInput,
    AssessmentAnalyticsRead,
    AssessmentCreate,
    AssessmentDropOffPoint,
    AssessmentVolumePoint,
    QuestionCreate,
    QuestionOptionInput,
    QuestionOptionPatchInput,
    QuestionUpdate,
)
from app.schemas.assessment_threshold import AssessmentThresholdUpsert
from app.services import ab_variant_service, ai_insight_service

# Tips are surfaced for answers scoring below this threshold.
TIP_SCORE_THRESHOLD = 60.0

# Fallback cutoffs when no AssessmentThreshold config exists for a scope — these
# are the same values that were previously hardcoded directly in tier_for_score.
DEFAULT_HIGHLY_ELIGIBLE_MIN = 80.0
DEFAULT_LIKELY_ELIGIBLE_MIN = 60.0
DEFAULT_BORDERLINE_MIN = 40.0


async def resolve_tier(
    session: AsyncSession, score: float, country: str, visa_type: str
) -> EligibilityTier:
    """Score -> tier, using the most specific configured threshold for this
    country/visa type (exact match > global > hardcoded default)."""
    result = await session.execute(
        select(AssessmentThreshold)
        .where(AssessmentThreshold.is_active.is_(True))
        .where(
            or_(
                AssessmentThreshold.country_code.is_(None),
                AssessmentThreshold.country_code == country.upper(),
            )
        )
        .where(
            or_(
                AssessmentThreshold.visa_type.is_(None),
                AssessmentThreshold.visa_type == visa_type.lower(),
            )
        )
    )
    candidates = list(result.scalars().all())

    def _specificity(t: AssessmentThreshold) -> int:
        return (t.country_code is not None) + (t.visa_type is not None)

    threshold = max(candidates, key=_specificity, default=None)

    highly_eligible_min = (
        threshold.highly_eligible_min if threshold else DEFAULT_HIGHLY_ELIGIBLE_MIN
    )
    likely_eligible_min = (
        threshold.likely_eligible_min if threshold else DEFAULT_LIKELY_ELIGIBLE_MIN
    )
    borderline_min = threshold.borderline_min if threshold else DEFAULT_BORDERLINE_MIN

    if score >= highly_eligible_min:
        return EligibilityTier.highly_eligible
    if score >= likely_eligible_min:
        return EligibilityTier.likely_eligible
    if score >= borderline_min:
        return EligibilityTier.borderline
    return EligibilityTier.low_eligibility


async def list_questions(
    session: AsyncSession, country: str, visa_type: str
) -> list[AssessmentQuestion]:
    """Active questions applying to the country/visa type (NULL scope = global)."""
    stmt = (
        select(AssessmentQuestion)
        .where(AssessmentQuestion.is_active.is_(True))
        .where(
            or_(
                AssessmentQuestion.country_code.is_(None),
                AssessmentQuestion.country_code == country.upper(),
            )
        )
        .where(
            or_(
                AssessmentQuestion.visa_type.is_(None),
                AssessmentQuestion.visa_type == visa_type.lower(),
            )
        )
        .order_by(AssessmentQuestion.display_order, AssessmentQuestion.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def start(session: AsyncSession, user_id: uuid.UUID, data: AssessmentCreate) -> Assessment:
    country = data.destination_country.upper()
    visa = data.visa_type.lower()
    variant = await ab_variant_service.pick_for_scope(session, country, visa)
    assessment = Assessment(
        user_id=user_id,
        destination_country=country,
        visa_type=visa,
        ab_variant_id=variant.id if variant else None,
        created_by=user_id,
    )
    session.add(assessment)
    await session.flush()
    await session.refresh(assessment)
    return assessment


async def get_for_user(
    session: AsyncSession, assessment_id: uuid.UUID, user_id: uuid.UUID
) -> Assessment:
    assessment = await session.get(Assessment, assessment_id)
    if assessment is None or assessment.user_id != user_id:
        raise NotFoundError("Assessment not found")
    return assessment


def _applicable(
    questions: list[AssessmentQuestion], selected_option_ids: set[uuid.UUID]
) -> list[AssessmentQuestion]:
    """Filter out adaptive questions whose trigger option was not selected."""
    return [
        question
        for question in questions
        if question.depends_on_option_id is None
        or question.depends_on_option_id in selected_option_ids
    ]


async def _persist_answers(
    session: AsyncSession,
    assessment: Assessment,
    answers: list[AnswerInput],
    questions_by_id: dict[uuid.UUID, AssessmentQuestion],
) -> list[AssessmentAnswer]:
    answer_rows: list[AssessmentAnswer] = []
    for answer in answers:
        question = questions_by_id.get(answer.question_id)
        if question is None:
            raise AppError("Unknown question submitted", code="invalid_answer")
        option = next((o for o in question.options if o.id == answer.option_id), None)
        if option is None:
            raise AppError("Option does not belong to its question", code="invalid_answer")
        answer_rows.append(
            AssessmentAnswer(
                assessment_id=assessment.id,
                question_id=answer.question_id,
                option_id=answer.option_id,
            )
        )
    assessment.answers = answer_rows
    session.add(assessment)
    await session.flush()
    return answer_rows


async def submit_answers(
    session: AsyncSession,
    assessment: Assessment,
    answers: list[AnswerInput],
    settings: Settings,
    *,
    complete: bool = True,
) -> Assessment:
    """Score the assessment from the submitted answers and mark it completed.

    When ``complete`` is False, answers are saved as progress only (status stays
    in_progress) so drop-off analytics can attribute abandonments to a question.
    """
    if assessment.status == AssessmentStatus.completed:
        raise AppError("Assessment already completed", code="assessment_completed")

    questions = await list_questions(session, assessment.destination_country, assessment.visa_type)
    if not questions:
        raise AppError("No questionnaire configured", code="no_questions")
    questions_by_id = {q.id: q for q in questions}

    if not complete:
        await _persist_answers(session, assessment, answers, questions_by_id)
        await session.refresh(assessment)
        return assessment

    answer_by_question: dict[uuid.UUID, AnswerInput] = {}
    for answer in answers:
        if answer.question_id not in questions_by_id:
            raise AppError("Unknown question submitted", code="invalid_answer")
        answer_by_question[answer.question_id] = answer

    selected_option_ids = {a.option_id for a in answer_by_question.values()}
    applicable = _applicable(questions, selected_option_ids)

    weighted_sum = 0.0
    weight_total = 0.0
    answered = 0
    category_totals: dict[str, list[float]] = defaultdict(list)
    tips: list[str] = []
    answer_rows: list[AssessmentAnswer] = []
    answered_pairs: list[tuple[AssessmentQuestion, AssessmentQuestionOption]] = []

    for question in applicable:
        submitted = answer_by_question.get(question.id)
        if submitted is None:
            continue
        option = next((o for o in question.options if o.id == submitted.option_id), None)
        if option is None:
            raise AppError("Option does not belong to its question", code="invalid_answer")

        answered += 1
        weighted_sum += option.score * question.weight
        weight_total += question.weight
        if question.category is not None:
            category_totals[question.category.value].append(option.score)
        if option.score < TIP_SCORE_THRESHOLD and option.improvement_tip:
            tips.append(option.improvement_tip)
        answered_pairs.append((question, option))
        answer_rows.append(
            AssessmentAnswer(
                assessment_id=assessment.id,
                question_id=question.id,
                option_id=option.id,
            )
        )

    if answered == 0:
        raise AppError("No valid answers submitted", code="invalid_answer")

    assessment.score = round(weighted_sum / weight_total, 2)
    assessment.tier = await resolve_tier(
        session, assessment.score, assessment.destination_country, assessment.visa_type
    )
    assessment.confidence = round(answered / len(applicable), 2)
    assessment.status = AssessmentStatus.completed
    assessment.completed_at = datetime.now(UTC)
    assessment.updated_by = assessment.user_id

    assessment.answers = answer_rows
    assessment.category_scores = [
        AssessmentCategoryScore(
            assessment_id=assessment.id,
            category=category,
            score=round(sum(scores) / len(scores), 2),
        )
        for category, scores in category_totals.items()
    ]
    assessment.tips = [AssessmentTip(assessment_id=assessment.id, tip=t) for t in tips]

    # Best-effort AI narrative — None (unconfigured/failed) leaves insights
    # empty and ai_summary NULL; the frontend falls back to improvement_tips.
    payload = await ai_insight_service.generate_insights(assessment, answered_pairs, settings)
    if payload is not None:
        assessment.ai_summary = payload.summary
        insight_rows: list[AssessmentInsight] = []
        for kind, texts in (
            (InsightKind.strength, payload.strengths),
            (InsightKind.weakness, payload.weaknesses),
            (InsightKind.missing_requirement, payload.missing_requirements),
        ):
            for order, text in enumerate(texts):
                insight_rows.append(
                    AssessmentInsight(
                        assessment_id=assessment.id,
                        kind=kind,
                        text=text,
                        display_order=order,
                    )
                )
        assessment.insights = insight_rows

    session.add(assessment)
    await session.flush()
    await session.refresh(assessment)

    from app.services import advisor_lead_service  # local import avoids a cycle at import time

    await advisor_lead_service.generate_for_assessment(session, assessment)

    return assessment


def list_for_user_stmt(user_id: uuid.UUID) -> Select[tuple[Assessment]]:
    return (
        select(Assessment)
        .where(Assessment.user_id == user_id)
        .order_by(Assessment.created_at.desc())
    )


# ── Admin question configuration (PRD §4.4 subset) ───────────────────────────


def list_questions_admin_stmt(
    country: str | None, visa_type: str | None
) -> Select[tuple[AssessmentQuestion]]:
    stmt = select(AssessmentQuestion).order_by(
        AssessmentQuestion.display_order, AssessmentQuestion.created_at
    )
    if country:
        stmt = stmt.where(AssessmentQuestion.country_code == country.upper())
    if visa_type:
        stmt = stmt.where(AssessmentQuestion.visa_type == visa_type.lower())
    return stmt


def _build_options(data: list[QuestionOptionInput]) -> list[AssessmentQuestionOption]:
    return [
        AssessmentQuestionOption(
            text=option.text,
            score=option.score,
            improvement_tip=option.improvement_tip,
            display_order=option.display_order,
        )
        for option in data
    ]


def _merge_options(
    question: AssessmentQuestion, incoming: list[QuestionOptionPatchInput]
) -> None:
    """Update by id, create when id absent, delete when an existing id is omitted."""
    if len(incoming) < 2:
        raise AppError(
            "A question must keep at least two options",
            code="too_few_options",
        )

    existing_by_id = {opt.id: opt for opt in question.options}
    merged: list[AssessmentQuestionOption] = []
    seen: set[uuid.UUID] = set()

    for item in incoming:
        if item.id is not None:
            current = existing_by_id.get(item.id)
            if current is None:
                raise NotFoundError("Option not found on this question")
            if item.id in seen:
                raise AppError("Duplicate option id in patch", code="duplicate_option_id")
            seen.add(item.id)
            patch = item.model_dump(exclude_unset=True, exclude={"id"})
            for field, value in patch.items():
                setattr(current, field, value)
            merged.append(current)
        else:
            assert item.text is not None  # validated by QuestionOptionPatchInput
            merged.append(
                AssessmentQuestionOption(
                    text=item.text,
                    score=0.0 if item.score is None else item.score,
                    improvement_tip=item.improvement_tip,
                    display_order=0 if item.display_order is None else item.display_order,
                )
            )

    # Reassign so delete-orphan drops options not present in the patch list.
    question.options = merged


async def create_question(
    session: AsyncSession, data: QuestionCreate, admin_id: uuid.UUID
) -> AssessmentQuestion:
    question = AssessmentQuestion(
        text=data.text,
        description=data.description,
        category=data.category,
        country_code=data.country_code.upper() if data.country_code else None,
        visa_type=data.visa_type.lower() if data.visa_type else None,
        weight=data.weight,
        display_order=data.display_order,
        is_active=data.is_active,
        depends_on_option_id=data.depends_on_option_id,
        options=_build_options(data.options),
        created_by=admin_id,
    )
    session.add(question)
    await session.flush()
    await session.refresh(question)
    return question


async def update_question(
    session: AsyncSession,
    question: AssessmentQuestion,
    data: QuestionUpdate,
    admin_id: uuid.UUID,
) -> AssessmentQuestion:
    fields = data.model_dump(exclude_unset=True)
    fields.pop("weightage_pct", None)
    if "options" in fields:
        fields.pop("options")
        _merge_options(question, data.options or [])
    if "country_code" in fields:
        code = fields.pop("country_code")
        question.country_code = code.upper() if code else None
    if "visa_type" in fields:
        vt = fields.pop("visa_type")
        question.visa_type = vt.lower() if vt else None
    for field, value in fields.items():
        setattr(question, field, value)
    question.updated_by = admin_id
    session.add(question)
    await session.flush()
    await session.refresh(question)
    return question


async def delete_question(session: AsyncSession, question: AssessmentQuestion) -> None:
    await session.delete(question)
    await session.flush()


# ── Threshold settings (PRD §3.4 AI Engine Management) ───────────────────────


async def get_threshold(
    session: AsyncSession, country: str | None, visa_type: str | None
) -> AssessmentThreshold | None:
    """The exact-scope threshold config, if one has been set (no fallback —
    callers that need scoring-time fallback semantics should use ``resolve_tier``)."""
    country_code = country.upper() if country else None
    visa = visa_type.lower() if visa_type else None
    stmt = select(AssessmentThreshold).where(
        AssessmentThreshold.country_code.is_(None)
        if country_code is None
        else AssessmentThreshold.country_code == country_code,
        AssessmentThreshold.visa_type.is_(None)
        if visa is None
        else AssessmentThreshold.visa_type == visa,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_threshold(
    session: AsyncSession, data: AssessmentThresholdUpsert, admin_id: uuid.UUID
) -> AssessmentThreshold:
    existing = await get_threshold(session, data.country_code, data.visa_type)
    if existing is not None:
        existing.highly_eligible_min = data.highly_eligible_min
        existing.likely_eligible_min = data.likely_eligible_min
        existing.borderline_min = data.borderline_min
        existing.is_active = data.is_active
        existing.updated_by = admin_id
        session.add(existing)
        await session.flush()
        await session.refresh(existing)
        return existing

    threshold = AssessmentThreshold(
        country_code=data.country_code.upper() if data.country_code else None,
        visa_type=data.visa_type.lower() if data.visa_type else None,
        highly_eligible_min=data.highly_eligible_min,
        likely_eligible_min=data.likely_eligible_min,
        borderline_min=data.borderline_min,
        is_active=data.is_active,
        created_by=admin_id,
    )
    session.add(threshold)
    await session.flush()
    await session.refresh(threshold)
    return threshold


# ── AI Analytics (PRD §3.4 AI Engine Management) ─────────────────────────────

PASS_TIERS = (EligibilityTier.highly_eligible, EligibilityTier.likely_eligible)
FAIL_TIERS = (EligibilityTier.borderline, EligibilityTier.low_eligibility)


async def get_analytics(
    session: AsyncSession,
    country: str | None = None,
    visa_type: str | None = None,
    days: int = 30,
) -> AssessmentAnalyticsRead:
    since = datetime.now(UTC) - timedelta(days=days)

    scope_filters = []
    if country:
        scope_filters.append(Assessment.destination_country == country.upper())
    if visa_type:
        scope_filters.append(Assessment.visa_type == visa_type.lower())

    base_stmt = select(Assessment).where(Assessment.created_at >= since, *scope_filters)
    started = list((await session.execute(base_stmt)).scalars().all())

    total_started = len(started)
    completed = [a for a in started if a.status == AssessmentStatus.completed]
    total_completed = len(completed)
    in_progress = total_started - total_completed

    pass_count = sum(1 for a in completed if a.tier in PASS_TIERS)
    fail_count = sum(1 for a in completed if a.tier in FAIL_TIERS)
    pass_rate = round(100 * pass_count / total_completed, 1) if total_completed else 0.0
    fail_rate = round(100 * fail_count / total_completed, 1) if total_completed else 0.0

    drop_off_rate = round(100 * in_progress / total_started, 1) if total_started else 0.0

    volume_counts: dict[str, int] = defaultdict(int)
    for a in started:
        volume_counts[a.created_at.date().isoformat()] += 1
    volume = [AssessmentVolumePoint(date=d, count=c) for d, c in sorted(volume_counts.items())]

    drop_off_points = await _drop_off_points(session, started, country, visa_type)

    return AssessmentAnalyticsRead(
        window_days=days,
        total_started=total_started,
        total_completed=total_completed,
        volume=volume,
        pass_rate=pass_rate,
        fail_rate=fail_rate,
        drop_off_count=in_progress,
        drop_off_rate=drop_off_rate,
        drop_off_points=drop_off_points,
    )


async def _drop_off_points(
    session: AsyncSession,
    started: list[Assessment],
    country: str | None,
    visa_type: str | None,
) -> list[AssessmentDropOffPoint]:
    """Attribute each abandoned assessment to the next unanswered question."""
    abandoned = [a for a in started if a.status == AssessmentStatus.in_progress]
    if not abandoned:
        return []

    # Load questions for the most common abandoned scope (or filter scope).
    scope_country = country.upper() if country else None
    scope_visa = visa_type.lower() if visa_type else None
    if scope_country is None and abandoned:
        scope_country = abandoned[0].destination_country
    if scope_visa is None and abandoned:
        scope_visa = abandoned[0].visa_type

    questions = await list_questions(
        session, scope_country or "US", scope_visa or "tourist"
    )
    if not questions:
        return [
            AssessmentDropOffPoint(
                question_id=None, label="Before first question", count=len(abandoned)
            )
        ]

    counts: dict[uuid.UUID | None, int] = defaultdict(int)
    labels: dict[uuid.UUID | None, str] = {None: "Before first question"}
    for q in questions:
        labels[q.id] = q.text[:80]

    for assessment in abandoned:
        answered_ids = {a.question_id for a in (assessment.answers or [])}
        drop_qid: uuid.UUID | None = None
        for q in questions:
            if q.id not in answered_ids:
                drop_qid = q.id
                break
        counts[drop_qid] += 1

    return [
        AssessmentDropOffPoint(question_id=qid, label=labels.get(qid, "Unknown"), count=c)
        for qid, c in counts.items()
        if c > 0
    ]
