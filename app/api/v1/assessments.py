"""AI eligibility assessment endpoints (PRD §3.4) — seeker-facing."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.exceptions import PermissionDeniedError
from app.db.session import SessionDep
from app.models.assessment import Assessment, AssessmentStatus, InsightKind
from app.models.user import User, UserRole
from app.schemas.assessment import (
    AnswersSubmit,
    AssessmentCreate,
    AssessmentRead,
    AssessmentSummaryRead,
    CategoryScoreRead,
    QuestionOptionRead,
    QuestionRead,
)
from app.schemas.response import Meta, ResponseEnvelope
from app.services import advisor_matching_service, assessment_service

router = APIRouter(prefix="/assessments", tags=["assessments"])


def _require_seeker(user: User) -> None:
    if user.role != UserRole.seeker:
        raise PermissionDeniedError("Seeker account required")


async def _build_read(session: SessionDep, assessment: Assessment) -> AssessmentRead:
    matched = (
        await advisor_matching_service.match(session, assessment)
        if assessment.status == AssessmentStatus.completed
        else []
    )
    strengths: list[str] = []
    weaknesses: list[str] = []
    missing_requirements: list[str] = []
    for insight in assessment.insights or []:
        if insight.kind == InsightKind.strength:
            strengths.append(insight.text)
        elif insight.kind == InsightKind.weakness:
            weaknesses.append(insight.text)
        else:
            missing_requirements.append(insight.text)
    return AssessmentRead(
        id=assessment.id,
        destination_country=assessment.destination_country,
        visa_type=assessment.visa_type,
        status=assessment.status,
        score=assessment.score,
        tier=assessment.tier,
        confidence=assessment.confidence,
        created_at=assessment.created_at,
        completed_at=assessment.completed_at,
        category_scores=[
            CategoryScoreRead(category=c.category, score=c.score)
            for c in (assessment.category_scores or [])
        ],
        improvement_tips=[t.tip for t in (assessment.tips or [])],
        strengths=strengths,
        weaknesses=weaknesses,
        missing_requirements=missing_requirements,
        ai_summary=assessment.ai_summary,
        matched_advisors=matched,
    )


@router.get("/questions", response_model=ResponseEnvelope[list[QuestionRead]])
async def list_questions(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    country: Annotated[str, Query(min_length=2, max_length=2)],
    visa_type: Annotated[str, Query(min_length=1, max_length=50)],
) -> ResponseEnvelope[list[QuestionRead]]:
    _require_seeker(current_user)
    questions = await assessment_service.list_questions(session, country, visa_type)
    return ResponseEnvelope[list[QuestionRead]](
        data=[
            QuestionRead(
                id=q.id,
                text=q.text,
                description=q.description,
                category=q.category,
                display_order=q.display_order,
                depends_on_option_id=q.depends_on_option_id,
                options=[
                    QuestionOptionRead(id=o.id, text=o.text, display_order=o.display_order)
                    for o in q.options
                ],
            )
            for q in questions
        ],
        meta=Meta(request_id=request_id),
    )


@router.post("", status_code=201, response_model=ResponseEnvelope[AssessmentRead])
async def start_assessment(
    data: AssessmentCreate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AssessmentRead]:
    _require_seeker(current_user)
    assessment = await assessment_service.start(session, current_user.id, data)
    return ResponseEnvelope[AssessmentRead](
        data=await _build_read(session, assessment),
        meta=Meta(request_id=request_id),
    )


@router.post("/{assessment_id}/answers", response_model=ResponseEnvelope[AssessmentRead])
async def submit_answers(
    assessment_id: uuid.UUID,
    data: AnswersSubmit,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AssessmentRead]:
    _require_seeker(current_user)
    assessment = await assessment_service.get_for_user(session, assessment_id, current_user.id)
    assessment = await assessment_service.submit_answers(
        session, assessment, data.answers, settings
    )
    return ResponseEnvelope[AssessmentRead](
        data=await _build_read(session, assessment),
        meta=Meta(request_id=request_id),
    )


@router.get("/{assessment_id}", response_model=ResponseEnvelope[AssessmentRead])
async def get_assessment(
    assessment_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AssessmentRead]:
    _require_seeker(current_user)
    assessment = await assessment_service.get_for_user(session, assessment_id, current_user.id)
    return ResponseEnvelope[AssessmentRead](
        data=await _build_read(session, assessment),
        meta=Meta(request_id=request_id),
    )


@router.get("", response_model=ResponseEnvelope[list[AssessmentSummaryRead]])
async def list_my_assessments(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[AssessmentSummaryRead]]:
    _require_seeker(current_user)
    stmt = assessment_service.list_for_user_stmt(current_user.id)
    assessments, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[AssessmentSummaryRead]](
        data=[
            AssessmentSummaryRead(
                id=a.id,
                destination_country=a.destination_country,
                visa_type=a.visa_type,
                status=a.status,
                score=a.score,
                tier=a.tier,
                created_at=a.created_at,
                completed_at=a.completed_at,
            )
            for a in assessments
        ],
        meta=page_meta(params, total, request_id),
    )
