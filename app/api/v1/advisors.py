"""Advisor endpoints: public profile discovery and self-management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import (
    CurrentPrincipal,
    CurrentUser,
    Principal,
    RequestIdDep,
    SettingsDep,
    require_role,
    require_verified_advisor,
)
from app.api.pagination import PaginationDep, page_meta, paginate
from app.api.v1.bookings import _party_names, _read, _send_confirmations
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.file_storage import delete_file, resolve_url
from app.db.session import SessionDep
from app.models.advisor_lead import AdvisorLead, AdvisorLeadStatus
from app.models.advisor_profile import AdvisorProfile
from app.models.assessment import Assessment
from app.models.booking import Booking
from app.models.transaction import Transaction
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.advisor_credential import (
    AdvisorCredentialCreate,
    AdvisorCredentialFromKey,
    AdvisorCredentialRead,
)
from app.schemas.advisor_lead import AdvisorLeadRead
from app.schemas.advisor_profile import (
    AdvisorListingCard,
    AdvisorOnboardingSubmit,
    AdvisorProfilePublicRead,
    AdvisorProfileRead,
    AdvisorProfileUpdate,
)
from app.schemas.booking import AdvisorBookingCreate, BookingRead, ClientRead
from app.schemas.payment import (
    AdvisorConnectStatus,
    AdvisorEarnings,
    TransactionAdvisorRead,
    TransactionRead,
)
from app.schemas.payout import PayoutRequestCreate, PayoutRequestRead
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.seeker_document import (
    DocumentCommentCreate,
    DocumentCommentRead,
    SeekerDocumentRead,
    SeekerDocumentStatusUpdate,
)
from app.services import (
    advisor_credential_service,
    advisor_lead_service,
    advisor_profile_service,
    advisor_search_service,
    booking_service,
    payment_service,
    payout_service,
    review_service,
    seeker_document_service,
)
from app.services.advisor_search_service import AdvisorSearchFilters, SortOption

router = APIRouter(prefix="/advisors", tags=["advisors"])

VerifiedAdvisorDep = Annotated[Principal, Depends(require_verified_advisor)]


@router.get(
    "/me/profile",
    response_model=ResponseEnvelope[AdvisorProfileRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def get_my_advisor_profile(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorProfileRead]:
    profile = await advisor_profile_service.get_or_create(session, current_user.id)
    return ResponseEnvelope[AdvisorProfileRead](
        data=advisor_profile_service.build_read(profile),
        meta=Meta(request_id=request_id),
    )


@router.patch(
    "/me/profile",
    response_model=ResponseEnvelope[AdvisorProfileRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def update_my_advisor_profile(
    data: AdvisorProfileUpdate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorProfileRead]:
    profile = await advisor_profile_service.get_or_create(session, current_user.id)
    if profile.public_profile_slug is None:
        profile.public_profile_slug = await advisor_search_service.generate_unique_slug(
            session, current_user.full_name
        )
    profile = await advisor_profile_service.update(session, profile, data)
    return ResponseEnvelope[AdvisorProfileRead](
        data=advisor_profile_service.build_read(profile),
        meta=Meta(request_id=request_id),
    )


async def _profiles_by_user(
    session: SessionDep, users: list[User]
) -> dict[uuid.UUID, AdvisorProfile]:
    user_ids = [u.id for u in users]
    if not user_ids:
        return {}
    result = await session.execute(
        select(AdvisorProfile).where(AdvisorProfile.user_id.in_(user_ids))
    )
    return {p.user_id: p for p in result.scalars().all()}


@router.get("", response_model=ResponseEnvelope[list[AdvisorListingCard]])
async def list_advisors(
    params: PaginationDep,
    _principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
    q: Annotated[str | None, Query(max_length=100, description="Keyword search")] = None,
    country: Annotated[str | None, Query(max_length=2, description="Country expertise")] = None,
    visa_type: Annotated[str | None, Query(max_length=50)] = None,
    language: Annotated[str | None, Query(max_length=100)] = None,
    min_price: Annotated[float | None, Query(ge=0)] = None,
    max_price: Annotated[float | None, Query(ge=0)] = None,
    min_rating: Annotated[float | None, Query(ge=1, le=5)] = None,
    sort: Annotated[SortOption, Query()] = "newest",
) -> ResponseEnvelope[list[AdvisorListingCard]]:
    filters = AdvisorSearchFilters(
        q=q,
        country=country,
        visa_type=visa_type,
        language=language,
        min_price=min_price,
        max_price=max_price,
        min_rating=min_rating,
        sort=sort,
    )
    stmt = advisor_search_service.build_search_stmt(filters)
    users, total = await paginate(session, stmt, params)
    profiles_by_user = await _profiles_by_user(session, users)
    ratings = await review_service.rating_summaries(session, [u.id for u in users])

    return ResponseEnvelope[list[AdvisorListingCard]](
        data=[
            advisor_profile_service.build_listing_card(
                u, profiles_by_user.get(u.id), ratings.get(u.id)
            )
            for u in users
        ],
        meta=page_meta(params, total, request_id),
    )


@router.get("/featured", response_model=ResponseEnvelope[list[AdvisorListingCard]])
async def list_featured_advisors(
    params: PaginationDep,
    _principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[AdvisorListingCard]]:
    stmt = advisor_search_service.build_search_stmt(AdvisorSearchFilters(featured_only=True))
    users, total = await paginate(session, stmt, params)
    profiles_by_user = await _profiles_by_user(session, users)
    ratings = await review_service.rating_summaries(session, [u.id for u in users])

    return ResponseEnvelope[list[AdvisorListingCard]](
        data=[
            advisor_profile_service.build_listing_card(
                u, profiles_by_user.get(u.id), ratings.get(u.id)
            )
            for u in users
        ],
        meta=page_meta(params, total, request_id),
    )


@router.get("/slug/{slug}", response_model=ResponseEnvelope[AdvisorProfilePublicRead])
async def get_advisor_by_slug(
    slug: str,
    _principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorProfilePublicRead]:
    result = await session.execute(
        select(AdvisorProfile).where(AdvisorProfile.public_profile_slug == slug)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise NotFoundError("Advisor not found")
    user = await session.get(User, profile.user_id)
    if (
        user is None
        or user.role != UserRole.advisor
        or not user.is_active
        or user.verification_status != VerificationStatus.approved
    ):
        raise NotFoundError("Advisor not found")
    return ResponseEnvelope[AdvisorProfilePublicRead](
        data=advisor_profile_service.build_public_read(user, profile),
        meta=Meta(request_id=request_id),
    )


@router.get("/{advisor_id}", response_model=ResponseEnvelope[AdvisorProfilePublicRead])
async def get_advisor_public_profile(
    advisor_id: uuid.UUID,
    _principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorProfilePublicRead]:
    user = await session.get(User, advisor_id)
    if user is None or user.role != UserRole.advisor or not user.is_active:
        raise NotFoundError("Advisor not found")
    result = await session.execute(
        select(AdvisorProfile).where(AdvisorProfile.user_id == advisor_id)
    )
    profile = result.scalar_one_or_none()
    return ResponseEnvelope[AdvisorProfilePublicRead](
        data=advisor_profile_service.build_public_read(user, profile),
        meta=Meta(request_id=request_id),
    )


# ── Advisor onboarding ───────────────────────────────────────────────────────


@router.post(
    "/me/onboarding",
    status_code=200,
    response_model=ResponseEnvelope[AdvisorProfileRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def complete_advisor_onboarding(
    data: AdvisorOnboardingSubmit,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorProfileRead]:
    """Accept the complete advisor onboarding wizard payload in one shot.

    The frontend accumulates step data in browser storage and calls this
    endpoint once at the final wizard step.  For each document reference the
    server validates the file key belongs to this advisor, then creates the
    credential record pointing at the already-uploaded file.
    """
    # Build country list: base_country first (if given), then the rest
    countries: list[str] = list(data.country_expertise)
    if data.base_country and data.base_country not in countries:
        countries.insert(0, data.base_country)

    profile = await advisor_profile_service.get_or_create(session, current_user.id)
    if profile.public_profile_slug is None:
        profile.public_profile_slug = await advisor_search_service.generate_unique_slug(
            session, current_user.full_name
        )

    update = AdvisorProfileUpdate(
        title=data.title,
        bio=data.bio,
        years_of_experience=data.years_of_experience,
        successful_applications=data.successful_applications,
        visa_specializations=data.visa_specializations or None,
        country_expertise=countries or None,
        services=data.services or None,
    )
    profile = await advisor_profile_service.update(session, profile, update)

    # Create credential records for each uploaded document
    expected_prefix = f"credential/{current_user.id}/"
    for doc in data.documents:
        if not doc.file_key.startswith(expected_prefix):
            raise PermissionDeniedError("Invalid document key")
        file_url = f"/uploads/{doc.file_key}"
        await advisor_credential_service.create(
            session,
            current_user.id,
            AdvisorCredentialCreate(
                document_type=doc.document_type,
                document_name=doc.document_name,
                expiry_date=doc.expiry_date,
            ),
            file_url,
            None,
        )

    return ResponseEnvelope[AdvisorProfileRead](
        data=advisor_profile_service.build_read(profile),
        meta=Meta(request_id=request_id),
    )


# ── Credential management ────────────────────────────────────────────────────


@router.post(
    "/me/credentials",
    status_code=201,
    response_model=ResponseEnvelope[AdvisorCredentialRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def create_credential(
    data: AdvisorCredentialFromKey,
    current_user: CurrentUser,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorCredentialRead]:
    """Create a credential record for a file already uploaded via ``POST /uploads``.

    Upload the file first with ``category=credential``, then pass the returned
    ``file_key`` here alongside the document metadata.
    """
    if not data.file_key.startswith(f"credential/{current_user.id}/"):
        raise PermissionDeniedError("Invalid file key")
    file_url = f"/uploads/{data.file_key}"
    credential = await advisor_credential_service.create(
        session,
        current_user.id,
        AdvisorCredentialCreate(
            document_type=data.document_type,
            document_name=data.document_name,
            expiry_date=data.expiry_date,
        ),
        file_url,
        None,
    )
    out = AdvisorCredentialRead.model_validate(credential)
    out.file_url = resolve_url(out.file_url, settings)
    return ResponseEnvelope[AdvisorCredentialRead](
        data=out,
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/me/credentials",
    response_model=ResponseEnvelope[list[AdvisorCredentialRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def list_my_credentials(
    current_user: CurrentUser,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[AdvisorCredentialRead]]:
    credentials = await advisor_credential_service.list_by_user(session, current_user.id)
    out_list = [AdvisorCredentialRead.model_validate(c) for c in credentials]
    for out in out_list:
        out.file_url = resolve_url(out.file_url, settings)
    return ResponseEnvelope[list[AdvisorCredentialRead]](
        data=out_list,
        meta=Meta(request_id=request_id),
    )


# ── Stripe Connect ──────────────────────────────────────────────────────────


@router.post(
    "/me/stripe-connect",
    status_code=201,
    response_model=ResponseEnvelope[AdvisorConnectStatus],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def initiate_stripe_connect(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorConnectStatus]:
    """Create or resume Stripe Connect onboarding for the advisor."""
    status = await payment_service.create_connect_account(session, current_user, settings)
    return ResponseEnvelope[AdvisorConnectStatus](
        data=status,
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/me/stripe-connect",
    response_model=ResponseEnvelope[AdvisorConnectStatus],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def get_stripe_connect_status(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorConnectStatus]:
    """Check whether the advisor's Stripe Connect account is active."""
    status = await payment_service.get_connect_status(session, current_user.id, settings)
    return ResponseEnvelope[AdvisorConnectStatus](
        data=status,
        meta=Meta(request_id=request_id),
    )


# ── Earnings ─────────────────────────────────────────────────────────────────


@router.get(
    "/me/earnings",
    response_model=ResponseEnvelope[AdvisorEarnings],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def get_my_earnings(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorEarnings]:
    """Advisor earnings summary and full transaction history."""
    data = await payment_service.get_advisor_earnings(session, current_user.id)
    available_balance = await payout_service.get_available_balance(session, current_user.id)
    return ResponseEnvelope[AdvisorEarnings](
        data=AdvisorEarnings(
            total_earned_usd=data["total_earned_usd"],
            total_commission_paid_usd=data["total_commission_paid_usd"],
            available_balance_usd=available_balance,
            transactions=[
                TransactionRead.model_validate(t)
                for t in data["transactions"]  # type: ignore[attr-defined]
            ],
        ),
        meta=Meta(request_id=request_id),
    )


# ── Credential management ────────────────────────────────────────────────────


@router.delete(
    "/me/credentials/{credential_id}",
    status_code=204,
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def delete_credential(
    credential_id: uuid.UUID,
    current_user: CurrentUser,
    settings: SettingsDep,
    session: SessionDep,
) -> None:
    credential = await advisor_credential_service.get_by_id(session, credential_id)
    if credential is None or credential.user_id != current_user.id:
        raise NotFoundError("Credential not found")
    delete_file(credential.file_url, settings)
    await advisor_credential_service.delete(session, credential)


# ── AI-matched customer leads (PRD §3.4.3, inverse direction) ───────────────


async def _build_lead_read(session: SessionDep, lead: AdvisorLead) -> AdvisorLeadRead:
    seeker = await session.get(User, lead.seeker_id)
    assessment = await session.get(Assessment, lead.assessment_id)
    return AdvisorLeadRead(
        id=lead.id,
        seeker_id=lead.seeker_id,
        seeker_name=seeker.full_name if seeker else None,
        seeker_email=seeker.email if seeker else "",
        assessment_id=lead.assessment_id,
        destination_country=assessment.destination_country if assessment else "",
        visa_type=assessment.visa_type if assessment else "",
        match_score=lead.match_score,
        match_reasons=lead.match_reasons,
        status=lead.status,
        created_at=lead.created_at,
    )


@router.get(
    "/me/leads",
    response_model=ResponseEnvelope[list[AdvisorLeadRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def list_my_leads(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    status: AdvisorLeadStatus | None = None,
) -> ResponseEnvelope[list[AdvisorLeadRead]]:
    """AI-matched customer leads for this advisor, ranked by match score."""
    stmt = advisor_lead_service.list_for_advisor_stmt(current_user.id, status)
    leads, total = await paginate(session, stmt, params)
    data = [await _build_lead_read(session, lead) for lead in leads]
    return ResponseEnvelope[list[AdvisorLeadRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.get(
    "/me/leads/{lead_id}",
    response_model=ResponseEnvelope[AdvisorLeadRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def get_my_lead(
    lead_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorLeadRead]:
    """Lead detail, including the AI-generated match reasons. Marks the lead as viewed."""
    lead = await advisor_lead_service.get_for_advisor(session, lead_id, current_user.id)
    lead = await advisor_lead_service.mark_viewed(session, lead)
    return ResponseEnvelope[AdvisorLeadRead](
        data=await _build_lead_read(session, lead), meta=Meta(request_id=request_id)
    )


@router.post(
    "/me/leads/{lead_id}/contact",
    response_model=ResponseEnvelope[AdvisorLeadRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def contact_my_lead(
    lead_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorLeadRead]:
    """Record that the advisor reached out to this lead (status marker only —
    in-app chat requires an actual booking per PRD §3.7.1)."""
    lead = await advisor_lead_service.get_for_advisor(session, lead_id, current_user.id)
    lead = await advisor_lead_service.mark_contacted(session, lead, current_user.id)
    return ResponseEnvelope[AdvisorLeadRead](
        data=await _build_lead_read(session, lead), meta=Meta(request_id=request_id)
    )


@router.post(
    "/me/leads/{lead_id}/dismiss",
    response_model=ResponseEnvelope[AdvisorLeadRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def dismiss_my_lead(
    lead_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorLeadRead]:
    lead = await advisor_lead_service.get_for_advisor(session, lead_id, current_user.id)
    lead = await advisor_lead_service.dismiss(session, lead, current_user.id)
    return ResponseEnvelope[AdvisorLeadRead](
        data=await _build_lead_read(session, lead), meta=Meta(request_id=request_id)
    )


# ── Calendar view: advisor-initiated bookings + client picker ───────────────


@router.post(
    "/me/bookings",
    status_code=201,
    response_model=ResponseEnvelope[BookingRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def create_booking_for_client(
    data: AdvisorBookingCreate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    """Advisor books a consultation directly for one of their existing clients.

    Confirmed immediately (no accept step) — this is the calendar's "Book +/Create +"
    flow, distinct from a seeker's self-serve request-then-approve booking.
    """
    booking = await booking_service.create_by_advisor(session, current_user, data)
    await _send_confirmations(session, booking, settings)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/me/clients",
    response_model=ResponseEnvelope[list[ClientRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def list_my_clients(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> ResponseEnvelope[list[ClientRead]]:
    """Seekers with at least one prior booking with this advisor — powers the
    calendar's "Select Client" / "Search Client" picker."""
    stmt = booking_service.list_clients_stmt(current_user.id, q)
    clients, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[ClientRead]](
        data=[ClientRead(id=c.id, full_name=c.full_name, email=c.email) for c in clients],
        meta=page_meta(params, total, request_id),
    )


# ── Client document review (PRD §3.8) ────────────────────────────────────────


async def _assert_advisor_client_relationship(
    session: SessionDep, advisor_id: uuid.UUID, seeker_id: uuid.UUID
) -> None:
    """The "assigned advisor" gate: don't let an advisor discover or act on a
    seeker's documents without an existing booking relationship. 404s (not 403)
    so an unrelated seeker's existence isn't leaked."""
    if not await booking_service.has_client_relationship(session, advisor_id, seeker_id):
        raise NotFoundError("Client not found")


@router.get(
    "/me/clients/{seeker_id}/documents",
    response_model=ResponseEnvelope[list[SeekerDocumentRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def list_client_documents(
    seeker_id: uuid.UUID,
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[SeekerDocumentRead]]:
    await _assert_advisor_client_relationship(session, current_user.id, seeker_id)
    stmt = seeker_document_service.list_by_seeker_stmt(seeker_id)
    documents, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[SeekerDocumentRead]](
        data=[seeker_document_service.build_read(d, settings) for d in documents],
        meta=page_meta(params, total, request_id),
    )


@router.patch(
    "/me/clients/{seeker_id}/documents/{document_id}",
    response_model=ResponseEnvelope[SeekerDocumentRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def review_client_document(
    seeker_id: uuid.UUID,
    document_id: uuid.UUID,
    data: SeekerDocumentStatusUpdate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerDocumentRead]:
    await _assert_advisor_client_relationship(session, current_user.id, seeker_id)
    document = await seeker_document_service.get_for_seeker(session, document_id, seeker_id)
    document = await seeker_document_service.set_status(session, document, data, current_user.id)
    return ResponseEnvelope[SeekerDocumentRead](
        data=seeker_document_service.build_read(document, settings),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/me/clients/{seeker_id}/documents/{document_id}/comments",
    response_model=ResponseEnvelope[list[DocumentCommentRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def list_client_document_comments(
    seeker_id: uuid.UUID,
    document_id: uuid.UUID,
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[DocumentCommentRead]]:
    await _assert_advisor_client_relationship(session, current_user.id, seeker_id)
    document = await seeker_document_service.get_for_seeker(session, document_id, seeker_id)
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


@router.post(
    "/me/clients/{seeker_id}/documents/{document_id}/comments",
    status_code=201,
    response_model=ResponseEnvelope[DocumentCommentRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def add_client_document_comment(
    seeker_id: uuid.UUID,
    document_id: uuid.UUID,
    data: DocumentCommentCreate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[DocumentCommentRead]:
    await _assert_advisor_client_relationship(session, current_user.id, seeker_id)
    document = await seeker_document_service.get_for_seeker(session, document_id, seeker_id)
    comment = await seeker_document_service.add_comment(
        session, document, current_user.id, data.body
    )
    return ResponseEnvelope[DocumentCommentRead](
        data=seeker_document_service.build_comment_read(comment, current_user),
        meta=Meta(request_id=request_id),
    )


# ── Payments + payouts (PRD §3.10) ───────────────────────────────────────────


def _transaction_advisor_read(
    txn: Transaction, booking: Booking, seeker: User | None
) -> TransactionAdvisorRead:
    base = TransactionRead.model_validate(txn)
    return TransactionAdvisorRead(
        **base.model_dump(),
        seeker_id=booking.seeker_id,
        seeker_name=seeker.full_name if seeker else None,
        service_type=booking.service_type,
        scheduled_start=booking.scheduled_start,
    )


@router.get(
    "/me/payments",
    response_model=ResponseEnvelope[list[TransactionAdvisorRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def list_my_payments(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[TransactionAdvisorRead]]:
    """ "Payment of customers" list — one row per transaction on this advisor's bookings."""
    stmt = payment_service.list_for_advisor_stmt(current_user.id)
    txns, total = await paginate(session, stmt, params)

    data = []
    for txn in txns:
        booking = await session.get(Booking, txn.booking_id)
        seeker = await session.get(User, booking.seeker_id) if booking else None
        if booking is not None:
            data.append(_transaction_advisor_read(txn, booking, seeker))

    return ResponseEnvelope[list[TransactionAdvisorRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.get(
    "/me/payouts",
    response_model=ResponseEnvelope[list[PayoutRequestRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def list_my_payouts(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[PayoutRequestRead]]:
    stmt = payout_service.list_for_advisor_stmt(current_user.id)
    payouts, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[PayoutRequestRead]](
        data=[PayoutRequestRead.model_validate(p) for p in payouts],
        meta=page_meta(params, total, request_id),
    )


@router.post(
    "/me/payouts",
    status_code=201,
    response_model=ResponseEnvelope[PayoutRequestRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def request_payout(
    data: PayoutRequestCreate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[PayoutRequestRead]:
    payout = await payout_service.create_request(session, current_user, data, settings)
    return ResponseEnvelope[PayoutRequestRead](
        data=PayoutRequestRead.model_validate(payout),
        meta=Meta(request_id=request_id),
    )
