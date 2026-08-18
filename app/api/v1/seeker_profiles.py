"""Seeker profile endpoints — read and update own profile."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.countries import country_code
from app.core.exceptions import AppError, PermissionDeniedError
from app.core.file_storage import resolve_url
from app.core.visa_types import OptionalVisaType
from app.db.session import SessionDep
from app.models.seeker_document import DocumentCategory, SeekerDocumentStatus
from app.models.user import User, UserRole
from app.schemas.advisor_dashboard import DashboardWindow
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.seeker_dashboard import SeekerDashboardRead
from app.schemas.seeker_document import (
    DocumentCommentCreate,
    DocumentCommentRead,
    DocumentPortfolioSummary,
    SeekerDocumentCreate,
    SeekerDocumentRead,
    SeekerDocumentUpdate,
)
from app.schemas.seeker_profile import (
    OnboardingCompleteRead,
    OnboardingSubmit,
    SeekerProfileRead,
    SeekerProfileUpdate,
)
from app.schemas.visa_journey import VisaJourneyRead
from app.services import (
    ai_insight_service,
    seeker_dashboard_service,
    seeker_document_service,
    seeker_profile_service,
    seeker_recommendation_service,
    visa_journey_service,
)

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
        country_of_residence=data.country_of_residence,
        education_level=data.education_level,
        employment_status=data.employment_status,
        employer_name=data.employer_name,
    )
    profile = await seeker_profile_service.update(session, profile, update, settings)
    suggestions = await ai_insight_service.generate_onboarding_suggestions(data, settings)
    await seeker_recommendation_service.refresh_for_seeker(
        session,
        current_user.id,
        settings=settings,
    )
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
    prev_intent = (profile.intended_destination, profile.intended_visa_type)
    profile = await seeker_profile_service.update(session, profile, data, settings)
    if (profile.intended_destination, profile.intended_visa_type) != prev_intent:
        await seeker_recommendation_service.refresh_for_seeker(
            session,
            current_user.id,
            settings=settings,
        )
    return ResponseEnvelope[SeekerProfileRead](
        data=seeker_profile_service.build_read(profile, settings),
        meta=Meta(request_id=request_id),
    )


# ── Seeker home dashboard ────────────────────────────────────────────────────


@router.get(
    "/dashboard",
    response_model=ResponseEnvelope[SeekerDashboardRead],
    summary="Seeker home dashboard",
    description=(
        "Single source of truth for the seeker home screen — next-appointment "
        "banner, stat cards, per-stage visa journey chart, eligibility donut, "
        "and profile-based matched advisors. ``next_upcoming`` matches the GET /bookings "
        "field of the same name. "
        "Omitted ``visa_type`` is all visa types (same as GET /users/me/documents "
        "and /summary). An explicit ``visa_type`` scopes stats, journey timeline, "
        "eligibility, matched advisors, and ``documents_uploaded`` to that visa "
        "(untagged documents included). Missing ``visa_type`` is never defaulted "
        "from the profile or latest assessment. "
        "``eligibility_score`` is 0 when no completed assessment exists in scope. "
        "``days`` (7/30/90/180/365, optional) is independent of visa (omit = "
        "all-time) and combines with it when both are set: it windows eligibility, "
        "the donut, and ``assessment_id`` to assessments completed in-window "
        "(score 0 if none) and ``stats.documents_uploaded`` to in-window uploads. "
        "``matched_advisors`` is the profile-based Find Advisor cache "
        "(``seeker_advisor_recommendations``), not AI Assessment matches. "
        "``journey_stages``, progress percents, and ``next_upcoming`` are always "
        "current-state. ``window_days`` echoes the applied window (null = all-time)."
    ),
)
async def get_my_dashboard(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    visa_type: Annotated[OptionalVisaType, Query()] = None,
    country: Annotated[
        str | None,
        Query(min_length=2, max_length=2, description="ISO 3166-1 alpha-2 destination"),
    ] = None,
    days: Annotated[
        DashboardWindow | None,
        Query(description="Window: 7, 30, 90, 180, or 365 days; omitted = all-time"),
    ] = None,
) -> ResponseEnvelope[SeekerDashboardRead]:
    _require_seeker(current_user)
    if country is not None and country_code(country) is None:
        raise AppError("Unknown country code", code="invalid_country")

    dashboard = await seeker_dashboard_service.get_dashboard(
        session,
        current_user.id,
        settings,
        visa_type=visa_type,
        country=country.upper() if country else None,
        days=days,
    )
    return ResponseEnvelope[SeekerDashboardRead](
        data=dashboard,
        meta=Meta(request_id=request_id),
    )


# ── Visa journey tracking ────────────────────────────────────────────────────


@router.get(
    "/visa-journey",
    response_model=ResponseEnvelope[VisaJourneyRead],
    summary="Visa Journey Tracking",
    description=(
        "Derived stepper for Assessment → Advisor → Documentation → "
        "Application preparation → Submission. Defaults ``visa_type`` / ``country`` "
        "from the latest completed assessment (then the seeker profile) when omitted. "
        "``advisor_suggestion`` prefers open document requests, then the latest "
        "advisor chat message, then missing checklist items. "
        "Submit via ``POST /users/me/visa-journey/submit`` after Review is complete."
    ),
)
async def get_my_visa_journey(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    visa_type: Annotated[OptionalVisaType, Query()] = None,
    country: Annotated[
        str | None,
        Query(min_length=2, max_length=2, description="ISO 3166-1 alpha-2 destination"),
    ] = None,
) -> ResponseEnvelope[VisaJourneyRead]:
    _require_seeker(current_user)
    if country is not None and country_code(country) is None:
        raise AppError("Unknown country code", code="invalid_country")

    resolved_visa, resolved_country = await seeker_dashboard_service.resolve_scope(
        session,
        current_user.id,
        visa_type=visa_type,
        country=country.upper() if country else None,
    )
    journey = await visa_journey_service.get_journey(
        session,
        current_user.id,
        settings,
        visa_type=resolved_visa,
        country=resolved_country,
    )
    return ResponseEnvelope[VisaJourneyRead](
        data=journey,
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/visa-journey/submit",
    response_model=ResponseEnvelope[VisaJourneyRead],
    summary="Submit visa application",
)
async def submit_visa_application(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    visa_type: Annotated[OptionalVisaType, Query()] = None,
    country: Annotated[
        str | None,
        Query(min_length=2, max_length=2, description="ISO 3166-1 alpha-2 destination"),
    ] = None,
) -> ResponseEnvelope[VisaJourneyRead]:
    """Marks Submission complete. Requires the Review stage to be completed. Idempotent."""
    _require_seeker(current_user)
    if country is not None and country_code(country) is None:
        raise AppError("Unknown country code", code="invalid_country")
    resolved_visa, resolved_country = await seeker_dashboard_service.resolve_scope(
        session,
        current_user.id,
        visa_type=visa_type,
        country=country.upper() if country else None,
    )
    await visa_journey_service.submit_application(
        session,
        current_user.id,
        current_user.id,
        visa_type=resolved_visa,
        country=resolved_country,
    )
    journey = await visa_journey_service.get_journey(
        session,
        current_user.id,
        settings,
        visa_type=resolved_visa,
        country=resolved_country,
    )
    return ResponseEnvelope[VisaJourneyRead](
        data=journey,
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
        data=await seeker_document_service.build_read_enriched(
            session, document, settings, include_unread=True
        ),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/documents/summary",
    response_model=ResponseEnvelope[DocumentPortfolioSummary],
    summary="Document portfolio overview",
    description=(
        "Totals for overview cards (total / approved / under_review / missing) plus "
        "the required-category checklist and progress percent. "
        "The checklist is one row per required category (not per uploaded file); "
        "status is rolled up from all active docs in that category "
        "(approved > under_review > rejected > expired; empty → missing). "
        "Progress is required categories with ≥1 file / 5. "
        "Individual file names are on ``GET /users/me/documents``. "
        "Default is portfolio-wide. Optional ``visa_type`` scopes tallies to that "
        "visa (untagged docs included)."
    ),
)
async def get_my_documents_summary(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    visa_type: Annotated[OptionalVisaType, Query()] = None,
) -> ResponseEnvelope[DocumentPortfolioSummary]:
    _require_seeker(current_user)
    summary = await seeker_document_service.portfolio_summary(
        session, current_user.id, visa_type=visa_type
    )
    return ResponseEnvelope[DocumentPortfolioSummary](
        data=summary,
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/documents",
    response_model=ResponseEnvelope[list[SeekerDocumentRead]],
    summary="List my documents",
    description=(
        "Paginated portfolio list. Filter by ``category``, ``status``, ``visa_type``, "
        "``expiring_within_days``, or ``expires_before``. Untagged docs match any visa filter."
    ),
)
async def list_my_documents(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    category: Annotated[DocumentCategory | None, Query()] = None,
    status: Annotated[SeekerDocumentStatus | None, Query()] = None,
    visa_type: Annotated[OptionalVisaType, Query()] = None,
    expiring_within_days: Annotated[
        int | None,
        Query(
            ge=0,
            le=3650,
            description="Upcoming expiry: expires_at from today through today+N (excludes past)",
        ),
    ] = None,
    expires_before: Annotated[date | None, Query()] = None,
) -> ResponseEnvelope[list[SeekerDocumentRead]]:
    _require_seeker(current_user)
    await seeker_document_service.refresh_expired_statuses(session, current_user.id)
    stmt = seeker_document_service.list_by_seeker_stmt(
        current_user.id,
        category=category,
        status=status,
        visa_type=visa_type,
        expiring_within_days=expiring_within_days,
        expires_before=expires_before,
    )
    documents, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[SeekerDocumentRead]](
        data=await seeker_document_service.build_reads(
            session, list(documents), settings, include_unread=True
        ),
        meta=page_meta(params, total, request_id),
    )


@router.patch(
    "/documents/{document_id}",
    response_model=ResponseEnvelope[SeekerDocumentRead],
)
async def update_my_document(
    document_id: uuid.UUID,
    data: SeekerDocumentUpdate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerDocumentRead]:
    """Update metadata and/or replace the file in place (same document id)."""
    _require_seeker(current_user)
    document = await seeker_document_service.get_for_seeker(session, document_id, current_user.id)
    file_url: str | None = None
    if data.file_key is not None:
        expected_prefix = f"seeker_document/{current_user.id}/"
        if not data.file_key.startswith(expected_prefix):
            raise PermissionDeniedError("Invalid attachment key")
        file_url = resolve_url(f"/uploads/{data.file_key}", settings)
    document = await seeker_document_service.update_document(
        session,
        document,
        data,
        current_user.id,
        file_url=file_url,
        settings=settings,
    )
    return ResponseEnvelope[SeekerDocumentRead](
        data=await seeker_document_service.build_read_enriched(
            session, document, settings, include_unread=True
        ),
        meta=Meta(request_id=request_id),
    )


@router.delete("/documents/{document_id}", status_code=204)
async def delete_my_document(
    document_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
) -> None:
    """Soft-archive the document (removed from list/summary; file retained)."""
    _require_seeker(current_user)
    document = await seeker_document_service.get_for_seeker(session, document_id, current_user.id)
    await seeker_document_service.archive_document(session, document, current_user.id)


@router.post("/documents/{document_id}/comments/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_document_comments_read(
    document_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
) -> None:
    """Clear the unread-comments dot. Idempotent. Does not run on GET comments."""
    _require_seeker(current_user)
    document = await seeker_document_service.get_for_seeker(session, document_id, current_user.id)
    await seeker_document_service.mark_comments_read(session, document, current_user.id)


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
