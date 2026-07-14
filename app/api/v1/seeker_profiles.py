"""Seeker profile endpoints — read and update own profile."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.exceptions import PermissionDeniedError
from app.core.file_storage import resolve_url
from app.db.session import SessionDep
from app.models.user import User, UserRole
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.seeker_document import (
    DocumentCommentCreate,
    DocumentCommentRead,
    SeekerDocumentCreate,
    SeekerDocumentRead,
)
from app.schemas.seeker_profile import (
    OnboardingCompleteRead,
    OnboardingSubmit,
    SeekerProfileRead,
    SeekerProfileUpdate,
)
from app.services import ai_insight_service, seeker_document_service, seeker_profile_service

router = APIRouter(prefix="/users/me", tags=["seeker-profile"])


def _require_seeker(current_user: User) -> None:
    if current_user.role != UserRole.seeker:
        raise PermissionDeniedError("Seeker account required")


@router.get("/profile", response_model=ResponseEnvelope[SeekerProfileRead])
async def get_my_profile(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerProfileRead]:
    _require_seeker(current_user)
    profile = await seeker_profile_service.get_or_create(session, current_user.id)
    return ResponseEnvelope[SeekerProfileRead](
        data=seeker_profile_service.build_read(profile, settings),
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/onboarding", status_code=200, response_model=ResponseEnvelope[OnboardingCompleteRead]
)
async def complete_onboarding(
    data: OnboardingSubmit,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[OnboardingCompleteRead]:
    """Accept the complete onboarding wizard payload in one shot.

    The frontend accumulates step data in browser storage and calls this
    endpoint once at the final wizard step. Step 5 AI suggestions are
    generated from the submitted data and returned alongside the profile.
    """
    _require_seeker(current_user)
    profile = await seeker_profile_service.get_or_create(session, current_user.id)
    update = SeekerProfileUpdate(
        intended_visa_type=data.intended_visa_type,
        intended_destination=data.intended_destination,
        annual_income_band=data.annual_income_band,
        nationality=data.nationality,
        education_level=data.education_level,
        employment_status=data.employment_status,
        employer_name=data.employer_name,
    )
    profile = await seeker_profile_service.update(session, profile, update, settings)
    suggestions = await ai_insight_service.generate_onboarding_suggestions(data, settings)
    profile_read = seeker_profile_service.build_read(profile, settings)
    return ResponseEnvelope[OnboardingCompleteRead](
        data=OnboardingCompleteRead(**profile_read.model_dump(), ai_suggestions=suggestions),
        meta=Meta(request_id=request_id),
    )


@router.patch("/profile", response_model=ResponseEnvelope[SeekerProfileRead])
async def update_my_profile(
    data: SeekerProfileUpdate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerProfileRead]:
    _require_seeker(current_user)
    profile = await seeker_profile_service.get_or_create(session, current_user.id)
    profile = await seeker_profile_service.update(session, profile, data, settings)
    return ResponseEnvelope[SeekerProfileRead](
        data=seeker_profile_service.build_read(profile, settings),
        meta=Meta(request_id=request_id),
    )


# ── Document portfolio (PRD §3.8) ────────────────────────────────────────────


@router.post("/documents", status_code=201, response_model=ResponseEnvelope[SeekerDocumentRead])
async def upload_document(
    data: SeekerDocumentCreate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerDocumentRead]:
    _require_seeker(current_user)
    expected_prefix = f"seeker_document/{current_user.id}/"
    if not data.file_key.startswith(expected_prefix):
        raise PermissionDeniedError("Invalid attachment key")
    file_url = resolve_url(f"/uploads/{data.file_key}", settings)
    document = await seeker_document_service.create(session, current_user.id, data, file_url)
    return ResponseEnvelope[SeekerDocumentRead](
        data=seeker_document_service.build_read(document, settings),
        meta=Meta(request_id=request_id),
    )


@router.get("/documents", response_model=ResponseEnvelope[list[SeekerDocumentRead]])
async def list_my_documents(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[SeekerDocumentRead]]:
    _require_seeker(current_user)
    stmt = seeker_document_service.list_by_seeker_stmt(current_user.id)
    documents, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[SeekerDocumentRead]](
        data=[seeker_document_service.build_read(d, settings) for d in documents],
        meta=page_meta(params, total, request_id),
    )


@router.post(
    "/documents/{document_id}/comments",
    status_code=201,
    response_model=ResponseEnvelope[DocumentCommentRead],
)
async def add_document_comment(
    document_id: uuid.UUID,
    data: DocumentCommentCreate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[DocumentCommentRead]:
    _require_seeker(current_user)
    document = await seeker_document_service.get_for_seeker(session, document_id, current_user.id)
    comment = await seeker_document_service.add_comment(
        session, document, current_user.id, data.body
    )
    return ResponseEnvelope[DocumentCommentRead](
        data=seeker_document_service.build_comment_read(comment, current_user),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/documents/{document_id}/comments",
    response_model=ResponseEnvelope[list[DocumentCommentRead]],
)
async def list_document_comments(
    document_id: uuid.UUID,
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[DocumentCommentRead]]:
    _require_seeker(current_user)
    document = await seeker_document_service.get_for_seeker(session, document_id, current_user.id)
    stmt = seeker_document_service.list_comments_stmt(document.id)
    comments, total = await paginate(session, stmt, params)

    authors: dict[uuid.UUID, User] = {}
    for comment in comments:
        if comment.author_id not in authors:
            author = await session.get(User, comment.author_id)
            if author is not None:
                authors[comment.author_id] = author

    return ResponseEnvelope[list[DocumentCommentRead]](
        data=[
            seeker_document_service.build_comment_read(c, authors.get(c.author_id))
            for c in comments
        ],
        meta=page_meta(params, total, request_id),
    )
