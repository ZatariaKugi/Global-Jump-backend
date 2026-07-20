"""Admin-only endpoints: advisor verification and user account management."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends

from app.api.deps import CurrentPrincipal, RequestIdDep, SettingsDep, require_role
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.file_storage import resolve_url
from app.core.visa_types import OptionalVisaType
from app.db.session import SessionDep
from app.models.advisor_credential import CredentialStatus
from app.models.assessment import AssessmentQuestion
from app.models.assessment_threshold import AssessmentThreshold
from app.models.booking import BookingStatus
from app.models.eligibility_rule import EligibilityRule
from app.models.payout_request import PayoutStatus
from app.models.review import Review
from app.models.support_ticket import TicketPriority
from app.models.ticket_message import TicketMessageAttachment
from app.models.transaction import TransactionStatus
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.ab_variant import AbVariantCreate, AbVariantRead, AbVariantUpdate
from app.schemas.admin import FeatureFlagUpdate, VerificationStatusUpdate
from app.schemas.advisor import AdvisorRead
from app.schemas.advisor_admin import (
    AdvisorEarningsSummaryRead,
    AdvisorManagementDetailRead,
    AdvisorManagementListRead,
    AdvisorSessionRead,
    BulkCredentialReview,
    VerificationQueueRead,
)
from app.schemas.advisor_credential import AdvisorCredentialRead, CredentialStatusUpdate
from app.schemas.advisor_profile import AdvisorProfileRead
from app.schemas.analytics import (
    AdvisorAnalyticsRead,
    AIAnalyticsRead,
    EngagementAnalyticsRead,
    FinanceAnalyticsRead,
    OverviewAnalyticsRead,
)
from app.schemas.assessment import (
    AssessmentAnalyticsRead,
    QuestionAdminRead,
    QuestionCreate,
    QuestionOptionAdminRead,
    QuestionUpdate,
)
from app.schemas.assessment_threshold import AssessmentThresholdRead, AssessmentThresholdUpsert
from app.schemas.booking import BookingDetailsRead
from app.schemas.conversation import FlaggedMessageRead
from app.schemas.dashboard import ActivityFeedItemRead, DashboardSummaryRead
from app.schemas.eligibility_rule import (
    EligibilityRuleCreate,
    EligibilityRuleRead,
    EligibilityRuleUpdate,
)
from app.schemas.engine_settings import EngineSettingsRead
from app.schemas.impersonation import ImpersonationRead
from app.schemas.matching_weights import MatchingWeightsRead, MatchingWeightsUpdate
from app.schemas.payment import (
    InvoiceRead,
    PaymentSummaryRead,
    RefundCreate,
    TransactionFinanceRead,
)
from app.schemas.payout import PayoutDecision, PayoutRequestRead
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.review import (
    AdvisorReviewsTabRead,
    ModerationDecision,
    ReviewAdminRead,
)
from app.schemas.seeker_admin import SeekerCreate, SeekerDetailRead, SeekerListRead
from app.schemas.seeker_document import SeekerDocumentRead, SeekerDocumentStatusUpdate
from app.schemas.support_ticket import TicketCreate, TicketRead, TicketUpdate
from app.schemas.ticket_message import TicketMessageRead, TicketMessageSend
from app.schemas.transaction_event import TransactionEventRead
from app.schemas.user_admin import AccountStatus, UserDetailRead, UserListRead
from app.services import (
    ab_variant_service,
    advisor_admin_service,
    advisor_credential_service,
    advisor_profile_service,
    analytics_service,
    assessment_service,
    booking_service,
    conversation_service,
    dashboard_service,
    eligibility_rule_service,
    impersonation_service,
    matching_weights_service,
    payment_service,
    payout_service,
    review_service,
    seeker_admin_service,
    seeker_document_service,
    support_ticket_service,
    ticket_message_service,
    user_admin_service,
    verification_queue_service,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_role(UserRole.admin))],
)


@router.get("/advisors", response_model=ResponseEnvelope[list[AdvisorManagementListRead]])
async def list_advisors(
    params: PaginationDep,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    status: VerificationStatus | None = None,
    search: str | None = None,
    visa_type: OptionalVisaType = None,
) -> ResponseEnvelope[list[AdvisorManagementListRead]]:
    """Advisor Management list: Advisor, Expertise, Status, Registration Date,
    Sessions, Rating. Optional ``visa_type`` filters by PRD specialization enum."""
    stmt = advisor_admin_service.list_advisors_stmt(search, status, visa_type)
    advisors, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[AdvisorManagementListRead]](
        data=await advisor_admin_service.build_list_read(session, advisors, settings),
        meta=page_meta(params, total, request_id),
    )


@router.patch("/advisors/{advisor_id}/verification", response_model=ResponseEnvelope[AdvisorRead])
async def update_advisor_verification(
    advisor_id: uuid.UUID,
    body: VerificationStatusUpdate,
    admin_principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
    settings: SettingsDep,
) -> ResponseEnvelope[AdvisorRead]:
    """Update an advisor's verification status.

    Setting status to ``approved`` activates the account (``is_active=True``),
    marks any pending credential documents as verified (so the advisor leaves
    ``GET /admin/verification-queue``), and sends a welcome email. Setting to
    ``rejected`` rejects any still-pending documents and blocks subsequent
    login/refresh with a contact-support message. Setting to ``pending`` /
    ``under_review`` leaves onboarding inactive and reopens credential
    documents to ``pending`` so the advisor returns to the verification queue.
    Rejection emails include an optional ``reason``.
    """
    from app.core.exceptions import NotFoundError
    from app.services import advisor_credential_service
    from app.services.email_service import (
        send_advisor_pending_email,
        send_advisor_rejected_email,
        send_advisor_welcome_email,
    )

    advisor = await session.get(User, advisor_id)
    if advisor is None or advisor.role != UserRole.advisor:
        raise NotFoundError("Advisor not found")

    previous_status = advisor.verification_status
    advisor.verification_status = body.status
    if body.status == VerificationStatus.approved:
        advisor.is_active = True
        advisor.is_suspended = False
        advisor.pre_suspend_verification_status = None
        await advisor_credential_service.resolve_pending(
            session,
            advisor_id,
            CredentialStatus.verified,
            admin_principal.id,
            admin_note=body.reason,
        )
    elif body.status == VerificationStatus.rejected:
        # Keep password / refresh credentials usable after rejection email.
        advisor.is_active = True
        await advisor_credential_service.resolve_pending(
            session,
            advisor_id,
            CredentialStatus.rejected,
            admin_principal.id,
            admin_note=body.reason,
        )
    elif body.status in (VerificationStatus.pending, VerificationStatus.under_review):
        advisor.is_active = False
        await advisor_credential_service.reopen_for_review(
            session,
            advisor_id,
            admin_principal.id,
            admin_note=body.reason,
        )
    session.add(advisor)
    await session.flush()
    await session.refresh(advisor)

    if (
        body.status == VerificationStatus.approved
        and previous_status != VerificationStatus.approved
    ):
        await send_advisor_welcome_email(
            advisor.email,
            advisor.full_name or "",
            settings,
        )
    elif (
        body.status == VerificationStatus.rejected
        and previous_status != VerificationStatus.rejected
    ):
        await send_advisor_rejected_email(
            advisor.email,
            advisor.full_name or "",
            settings,
            reason=body.reason,
        )
    elif (
        body.status == VerificationStatus.pending
        and previous_status != VerificationStatus.pending
    ):
        await send_advisor_pending_email(
            advisor.email,
            advisor.full_name or "",
            settings,
        )

    return ResponseEnvelope[AdvisorRead](
        data=AdvisorRead.model_validate(advisor),
        meta=Meta(request_id=request_id),
    )


@router.patch(
    "/advisors/{advisor_id}/feature",
    response_model=ResponseEnvelope[AdvisorProfileRead],
)
async def update_advisor_featured(
    advisor_id: uuid.UUID,
    body: FeatureFlagUpdate,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorProfileRead]:
    """Feature or un-feature an advisor on the homepage (admin-curated, PRD §3.5)."""
    from app.core.exceptions import NotFoundError

    advisor = await session.get(User, advisor_id)
    if advisor is None or advisor.role != UserRole.advisor:
        raise NotFoundError("Advisor not found")

    profile = await advisor_profile_service.get_or_create(session, advisor_id)
    profile.is_featured = body.is_featured
    session.add(profile)
    await session.flush()
    await session.refresh(profile)
    return ResponseEnvelope[AdvisorProfileRead](
        data=await advisor_profile_service.build_enriched_read(
            session, profile, advisor, settings
        ),
        meta=Meta(request_id=request_id),
    )


@router.post("/users/{user_id}/suspend", response_model=ResponseEnvelope[UserDetailRead])
async def suspend_user(
    user_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UserDetailRead]:
    """Soft-suspend. Advisors get ``verification_status=suspended`` for the UI badge."""
    await user_admin_service.suspend_account(session, user_id)
    data = await user_admin_service.get_user_detail(session, user_id)
    return ResponseEnvelope[UserDetailRead](
        data=data,
        meta=Meta(request_id=request_id),
    )


@router.post("/users/{user_id}/reactivate", response_model=ResponseEnvelope[UserDetailRead])
async def reactivate_user(
    user_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UserDetailRead]:
    """Clear soft-suspend and restore the advisor's prior ``verification_status``."""
    await user_admin_service.reactivate_account(session, user_id)
    data = await user_admin_service.get_user_detail(session, user_id)
    return ResponseEnvelope[UserDetailRead](
        data=data,
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/users/{user_id}/impersonate",
    response_model=ResponseEnvelope[ImpersonationRead],
)
async def impersonate_user(
    user_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    admin_principal: CurrentPrincipal,
) -> ResponseEnvelope[ImpersonationRead]:
    """Start an impersonation session as the target user (admin only).

    Returns a short-lived access token that authenticates as ``user_id``. The
    frontend should store the admin's own tokens separately, swap to this
    token while impersonating, and discard it on "Exit impersonation".

    Cannot impersonate admins or inactive accounts.
    """
    from app.core.exceptions import AuthenticationError

    if admin_principal.user is None:
        raise AuthenticationError("A local admin account is required")

    data = await impersonation_service.impersonate(
        session,
        target_user_id=user_id,
        admin=admin_principal.user,
        settings=settings,
    )
    return ResponseEnvelope[ImpersonationRead](
        data=data,
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/advisors/{advisor_id}/credentials",
    response_model=ResponseEnvelope[list[AdvisorCredentialRead]],
)
async def list_advisor_credentials(
    advisor_id: uuid.UUID,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
    status: CredentialStatus | None = None,
) -> ResponseEnvelope[list[AdvisorCredentialRead]]:
    """List credential documents submitted by an advisor (including archived
    unless ``status`` narrows it). The Verification Queue's Documents panel
    calls this with ``?status=pending``."""
    credentials = await advisor_credential_service.get_for_advisor_admin(
        session, advisor_id, status
    )
    out_list = [AdvisorCredentialRead.model_validate(c) for c in credentials]
    for out in out_list:
        out.file_url = resolve_url(out.file_url, settings)
    return ResponseEnvelope[list[AdvisorCredentialRead]](
        data=out_list,
        meta=Meta(request_id=request_id),
    )


@router.patch(
    "/advisors/{advisor_id}/credentials/{credential_id}",
    response_model=ResponseEnvelope[AdvisorCredentialRead],
)
async def review_advisor_credential(
    advisor_id: uuid.UUID,
    credential_id: uuid.UUID,
    body: CredentialStatusUpdate,
    admin_principal: CurrentPrincipal,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorCredentialRead]:
    """Verify or reject an advisor credential document."""
    from app.core.exceptions import NotFoundError

    credential = await advisor_credential_service.get_by_id(session, credential_id)
    if credential is None or credential.user_id != advisor_id:
        raise NotFoundError("Credential not found")

    advisor = await session.get(User, advisor_id)
    if advisor is None or advisor.role != UserRole.advisor:
        raise NotFoundError("Advisor not found")

    credential = await advisor_credential_service.update_status(
        session, credential, body, admin_principal.id
    )
    out = AdvisorCredentialRead.model_validate(credential)
    out.file_url = resolve_url(out.file_url, settings)
    return ResponseEnvelope[AdvisorCredentialRead](
        data=out,
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/advisors/{advisor_id}/credentials/bulk-review",
    response_model=ResponseEnvelope[list[AdvisorCredentialRead]],
)
async def bulk_review_advisor_credentials(
    advisor_id: uuid.UUID,
    body: BulkCredentialReview,
    admin_principal: CurrentPrincipal,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[AdvisorCredentialRead]]:
    """Verification Queue's bottom Approve/Reject buttons — acts on every
    currently-pending credential for one advisor at once."""
    from app.core.exceptions import NotFoundError

    advisor = await session.get(User, advisor_id)
    if advisor is None or advisor.role != UserRole.advisor:
        raise NotFoundError("Advisor not found")

    credentials = await advisor_credential_service.bulk_update_status(
        session, advisor_id, body, admin_principal.id
    )
    out_list = [AdvisorCredentialRead.model_validate(c) for c in credentials]
    for out in out_list:
        out.file_url = resolve_url(out.file_url, settings)
    return ResponseEnvelope[list[AdvisorCredentialRead]](
        data=out_list,
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/users/{user_id}/activate",
    response_model=ResponseEnvelope[UserDetailRead],
    deprecated=True,
)
async def activate_user(
    user_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UserDetailRead]:
    """Alias of ``POST /users/{user_id}/reactivate`` — prefer ``/reactivate``."""
    return await reactivate_user(user_id, session, request_id)


# ── Advisor Management (Screen A) ────────────────────────────────────────────


@router.get(
    "/advisors/{advisor_id}",
    response_model=ResponseEnvelope[AdvisorManagementDetailRead],
)
async def get_advisor_detail(
    advisor_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorManagementDetailRead]:
    """Detail page's Overview tab."""
    data = await advisor_admin_service.get_advisor_detail(session, advisor_id, settings)
    return ResponseEnvelope[AdvisorManagementDetailRead](
        data=data, meta=Meta(request_id=request_id)
    )


@router.get(
    "/advisors/{advisor_id}/sessions",
    response_model=ResponseEnvelope[list[AdvisorSessionRead]],
)
async def list_advisor_sessions(
    advisor_id: uuid.UUID,
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    status: BookingStatus | None = None,
) -> ResponseEnvelope[list[AdvisorSessionRead]]:
    """Detail page's Session History tab."""
    stmt = booking_service.list_for_user_stmt(advisor_id, UserRole.advisor, status=status)
    bookings, total = await paginate(session, stmt, params)
    data = await advisor_admin_service.build_session_reads(session, bookings)
    return ResponseEnvelope[list[AdvisorSessionRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.get(
    "/advisors/{advisor_id}/earnings",
    response_model=ResponseEnvelope[AdvisorEarningsSummaryRead],
)
async def get_advisor_earnings_summary(
    advisor_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorEarningsSummaryRead]:
    """Detail page's Earnings tab — summary cards + table rows."""
    data = await advisor_admin_service.get_earnings_summary(session, advisor_id)
    return ResponseEnvelope[AdvisorEarningsSummaryRead](data=data, meta=Meta(request_id=request_id))


@router.get(
    "/advisors/{advisor_id}/earnings/transactions",
    response_model=ResponseEnvelope[list[TransactionFinanceRead]],
)
async def list_advisor_earnings_transactions(
    advisor_id: uuid.UUID,
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[TransactionFinanceRead]]:
    """Detail page's Earnings tab — transaction history sub-list."""
    stmt = payment_service.list_for_advisor_stmt(advisor_id)
    txns, total = await paginate(session, stmt, params)
    data = [await payment_service.finance_read(session, t) for t in txns]
    return ResponseEnvelope[list[TransactionFinanceRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.get(
    "/advisors/{advisor_id}/earnings/payouts",
    response_model=ResponseEnvelope[list[PayoutRequestRead]],
)
async def list_advisor_earnings_payouts(
    advisor_id: uuid.UUID,
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    status: PayoutStatus | None = None,
) -> ResponseEnvelope[list[PayoutRequestRead]]:
    """Detail page's Earnings tab — payout history sub-list."""
    stmt = payout_service.list_for_advisor_stmt(advisor_id, status)
    payouts, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[PayoutRequestRead]](
        data=[PayoutRequestRead.model_validate(p) for p in payouts],
        meta=page_meta(params, total, request_id),
    )


@router.get(
    "/advisors/{advisor_id}/reviews",
    response_model=ResponseEnvelope[AdvisorReviewsTabRead],
)
async def list_advisor_reviews(
    advisor_id: uuid.UUID,
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    flagged: bool | None = None,
    visa_type: OptionalVisaType = None,
) -> ResponseEnvelope[AdvisorReviewsTabRead]:
    """Detail page's Reviews tab — rating summary + paginated review rows.

    Pass ``flagged=true`` to return only reviews awaiting moderation.
    Pass ``visa_type`` (PRD enum) to filter by the seeker's intended visa type.
    """
    stmt = review_service.list_public_stmt(
        advisor_id, flagged=flagged, visa_type=visa_type
    )
    reviews, total = await paginate(session, stmt, params)
    items = await review_service.build_enriched_reads(session, reviews)
    summary = await review_service.build_tab_summary(session, advisor_id)
    return ResponseEnvelope[AdvisorReviewsTabRead](
        data=AdvisorReviewsTabRead(summary=summary, items=items),
        meta=page_meta(params, total, request_id),
    )


@router.get(
    "/bookings/{booking_id}/details",
    response_model=ResponseEnvelope[BookingDetailsRead],
)
async def get_booking_details_admin(
    booking_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingDetailsRead]:
    """View Booking Details — used from admin Earnings / Session History drawers."""
    from app.core.exceptions import NotFoundError
    from app.models.booking import Booking

    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise NotFoundError("Booking not found")
    seeker = await session.get(User, booking.seeker_id)
    data = await booking_service.build_details(session, booking, seeker)
    return ResponseEnvelope[BookingDetailsRead](data=data, meta=Meta(request_id=request_id))


# ── Advisor Verification Queue (Screen B) ────────────────────────────────────


@router.get(
    "/verification-queue",
    response_model=ResponseEnvelope[list[VerificationQueueRead]],
)
async def list_verification_queue(
    params: PaginationDep,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[VerificationQueueRead]]:
    """Global, cross-advisor list of advisors with pending credential documents."""
    stmt = verification_queue_service.list_stmt()
    advisors, total = await paginate(session, stmt, params)
    data = await verification_queue_service.build_list_read(session, advisors, settings)
    return ResponseEnvelope[list[VerificationQueueRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


# ── Visa Seeker Management ───────────────────────────────────────────────────


@router.get("/seekers", response_model=ResponseEnvelope[list[SeekerListRead]])
async def list_seekers(
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    search: str | None = None,
    status: AccountStatus | None = None,
    study_visa: str | None = None,
    visa_type: OptionalVisaType = None,
) -> ResponseEnvelope[list[SeekerListRead]]:
    stmt = seeker_admin_service.list_seekers_stmt(search, status, study_visa, visa_type)
    users, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[SeekerListRead]](
        data=await seeker_admin_service.build_list_read(session, users),
        meta=page_meta(params, total, request_id),
    )


@router.post("/seekers", status_code=201, response_model=ResponseEnvelope[SeekerDetailRead])
async def create_seeker(
    body: SeekerCreate,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerDetailRead]:
    """Admin-invite a visa seeker; they set their own password via emailed link."""
    data = await seeker_admin_service.create_seeker(session, body, settings)
    return ResponseEnvelope[SeekerDetailRead](data=data, meta=Meta(request_id=request_id))


@router.get("/seekers/{seeker_id}", response_model=ResponseEnvelope[SeekerDetailRead])
async def get_seeker(
    seeker_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerDetailRead]:
    data = await seeker_admin_service.get_seeker_detail(session, seeker_id)
    return ResponseEnvelope[SeekerDetailRead](data=data, meta=Meta(request_id=request_id))


# ── User Management (broader, all-roles account directory) ──────────────────


@router.get("/users", response_model=ResponseEnvelope[list[UserListRead]])
async def list_users(
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    search: str | None = None,
    username: str | None = None,
    user_id: uuid.UUID | None = None,
    status: AccountStatus | None = None,
    role: UserRole | None = None,
    user_type: UserRole | None = None,
) -> ResponseEnvelope[list[UserListRead]]:
    """Unified seeker + advisor directory (one API for both).

    Search options (combinable):

    - ``search`` — matches full name, email, or user id (UUID / substring)
    - ``username`` — full_name ilike only
    - ``user_id`` — exact UUID
    - ``role`` / ``user_type`` — ``seeker`` or ``advisor`` (aliases; ``user_type`` wins)
    """
    effective_role = user_type if user_type is not None else role
    stmt = user_admin_service.list_users_stmt(
        search, status, effective_role, username=username, user_id=user_id
    )
    users, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[UserListRead]](
        data=await user_admin_service.build_list_read(session, users),
        meta=page_meta(params, total, request_id),
    )


@router.get("/users/{user_id}/profile", response_model=ResponseEnvelope[UserDetailRead])
async def get_user_profile(
    user_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UserDetailRead]:
    data = await user_admin_service.get_user_detail(session, user_id)
    return ResponseEnvelope[UserDetailRead](data=data, meta=Meta(request_id=request_id))


@router.post("/users/{user_id}/verify", response_model=ResponseEnvelope[UserDetailRead])
async def verify_user_account(
    user_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
    admin_principal: CurrentPrincipal,
) -> ResponseEnvelope[UserDetailRead]:
    """Admin override — directly marks the account's email as verified,
    bypassing the self-service token flow."""
    await user_admin_service.verify_account(session, user_id, admin_principal.id)
    data = await user_admin_service.get_user_detail(session, user_id)
    return ResponseEnvelope[UserDetailRead](data=data, meta=Meta(request_id=request_id))


@router.post("/users/{user_id}/reset-password", status_code=204)
async def trigger_password_reset(
    user_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    """Admin-triggered equivalent of the self-service /auth/forgot-password
    flow, skipping the email lookup since the admin already has the user_id."""
    await user_admin_service.trigger_password_reset(session, user_id, settings)


# ── Assessment question configuration (PRD §4.4 subset) ─────────────────────


def _question_admin_read(question: AssessmentQuestion) -> QuestionAdminRead:
    return QuestionAdminRead(
        id=question.id,
        text=question.text,
        description=question.description,
        category=question.category,
        country_code=question.country_code,
        visa_type=question.visa_type,
        weight=question.weight,
        weightage_pct=round(question.weight * 10.0, 1),
        display_order=question.display_order,
        is_active=question.is_active,
        depends_on_option_id=question.depends_on_option_id,
        options=[
            QuestionOptionAdminRead(
                id=o.id,
                text=o.text,
                score=o.score,
                improvement_tip=o.improvement_tip,
                display_order=o.display_order,
            )
            for o in question.options
        ],
    )


@router.post(
    "/assessment-questions",
    status_code=201,
    response_model=ResponseEnvelope[QuestionAdminRead],
)
async def create_assessment_question(
    body: QuestionCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[QuestionAdminRead]:
    question = await assessment_service.create_question(session, body, principal.id)
    return ResponseEnvelope[QuestionAdminRead](
        data=_question_admin_read(question),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/assessment-questions",
    response_model=ResponseEnvelope[list[QuestionAdminRead]],
)
async def list_assessment_questions(
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    country: str | None = None,
    visa_type: OptionalVisaType = None,
) -> ResponseEnvelope[list[QuestionAdminRead]]:
    stmt = assessment_service.list_questions_admin_stmt(country, visa_type)
    questions, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[QuestionAdminRead]](
        data=[_question_admin_read(q) for q in questions],
        meta=page_meta(params, total, request_id),
    )


@router.patch(
    "/assessment-questions/{question_id}",
    response_model=ResponseEnvelope[QuestionAdminRead],
)
async def update_assessment_question(
    question_id: uuid.UUID,
    body: QuestionUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[QuestionAdminRead]:
    from app.core.exceptions import NotFoundError

    question = await session.get(AssessmentQuestion, question_id)
    if question is None:
        raise NotFoundError("Question not found")
    question = await assessment_service.update_question(session, question, body, principal.id)
    return ResponseEnvelope[QuestionAdminRead](
        data=_question_admin_read(question),
        meta=Meta(request_id=request_id),
    )


@router.delete("/assessment-questions/{question_id}", status_code=204)
async def delete_assessment_question(
    question_id: uuid.UUID,
    session: SessionDep,
) -> None:
    from app.core.exceptions import NotFoundError

    question = await session.get(AssessmentQuestion, question_id)
    if question is None:
        raise NotFoundError("Question not found")
    await assessment_service.delete_question(session, question)


# ── Eligibility rules (PRD §3.4 AI Engine Management) ────────────────────────


def _eligibility_rule_read(rule: EligibilityRule) -> EligibilityRuleRead:
    return EligibilityRuleRead(
        id=rule.id,
        name=rule.name,
        description=rule.description,
        category=rule.category,
        country_code=rule.country_code,
        visa_type=rule.visa_type,
        points=rule.points,
        weightage_pct=rule.weightage_pct,
        is_active=rule.is_active,
    )


@router.post(
    "/eligibility-rules",
    status_code=201,
    response_model=ResponseEnvelope[EligibilityRuleRead],
)
async def create_eligibility_rule(
    body: EligibilityRuleCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[EligibilityRuleRead]:
    rule = await eligibility_rule_service.create(session, body, principal.id)
    return ResponseEnvelope[EligibilityRuleRead](
        data=_eligibility_rule_read(rule),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/eligibility-rules",
    response_model=ResponseEnvelope[list[EligibilityRuleRead]],
)
async def list_eligibility_rules(
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    country: str | None = None,
    visa_type: OptionalVisaType = None,
) -> ResponseEnvelope[list[EligibilityRuleRead]]:
    stmt = eligibility_rule_service.list_stmt(country, visa_type)
    rules, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[EligibilityRuleRead]](
        data=[_eligibility_rule_read(r) for r in rules],
        meta=page_meta(params, total, request_id),
    )


@router.patch(
    "/eligibility-rules/{rule_id}",
    response_model=ResponseEnvelope[EligibilityRuleRead],
)
async def update_eligibility_rule(
    rule_id: uuid.UUID,
    body: EligibilityRuleUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[EligibilityRuleRead]:
    from app.core.exceptions import NotFoundError

    rule = await session.get(EligibilityRule, rule_id)
    if rule is None:
        raise NotFoundError("Eligibility rule not found")
    rule = await eligibility_rule_service.update(session, rule, body, principal.id)
    return ResponseEnvelope[EligibilityRuleRead](
        data=_eligibility_rule_read(rule),
        meta=Meta(request_id=request_id),
    )


@router.delete("/eligibility-rules/{rule_id}", status_code=204)
async def delete_eligibility_rule(
    rule_id: uuid.UUID,
    session: SessionDep,
) -> None:
    from app.core.exceptions import NotFoundError

    rule = await session.get(EligibilityRule, rule_id)
    if rule is None:
        raise NotFoundError("Eligibility rule not found")
    await eligibility_rule_service.delete(session, rule)


# ── Threshold settings (PRD §3.4 AI Engine Management) ───────────────────────


def _threshold_read(threshold: AssessmentThreshold) -> AssessmentThresholdRead:
    return AssessmentThresholdRead(
        id=threshold.id,
        country_code=threshold.country_code,
        visa_type=threshold.visa_type,
        highly_eligible_min=threshold.highly_eligible_min,
        likely_eligible_min=threshold.likely_eligible_min,
        borderline_min=threshold.borderline_min,
        is_active=threshold.is_active,
    )


@router.get(
    "/assessment-thresholds",
    response_model=ResponseEnvelope[AssessmentThresholdRead | None],
)
async def get_assessment_threshold(
    session: SessionDep,
    request_id: RequestIdDep,
    country: str | None = None,
    visa_type: OptionalVisaType = None,
) -> ResponseEnvelope[AssessmentThresholdRead | None]:
    """The exact-scope threshold config, or null if this scope falls back to the
    global default (or the hardcoded 80/60/40 if no config exists at all)."""
    threshold = await assessment_service.get_threshold(session, country, visa_type)
    return ResponseEnvelope[AssessmentThresholdRead | None](
        data=_threshold_read(threshold) if threshold else None,
        meta=Meta(request_id=request_id),
    )


@router.put(
    "/assessment-thresholds",
    response_model=ResponseEnvelope[AssessmentThresholdRead],
)
async def upsert_assessment_threshold(
    body: AssessmentThresholdUpsert,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AssessmentThresholdRead]:
    threshold = await assessment_service.upsert_threshold(session, body, principal.id)
    return ResponseEnvelope[AssessmentThresholdRead](
        data=_threshold_read(threshold),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/assessment-analytics",
    response_model=ResponseEnvelope[AssessmentAnalyticsRead],
)
async def get_assessment_analytics(
    session: SessionDep,
    request_id: RequestIdDep,
    country: str | None = None,
    visa_type: OptionalVisaType = None,
    days: int = 30,
) -> ResponseEnvelope[AssessmentAnalyticsRead]:
    analytics = await assessment_service.get_analytics(session, country, visa_type, days)
    return ResponseEnvelope[AssessmentAnalyticsRead](
        data=analytics,
        meta=Meta(request_id=request_id),
    )


# ── Matching weights / A/B / engine settings (AI Engine Management) ───────────


@router.get("/matching-weights", response_model=ResponseEnvelope[MatchingWeightsRead])
async def get_matching_weights(
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[MatchingWeightsRead]:
    data = await matching_weights_service.get_read(session)
    return ResponseEnvelope[MatchingWeightsRead](data=data, meta=Meta(request_id=request_id))


@router.put("/matching-weights", response_model=ResponseEnvelope[MatchingWeightsRead])
async def upsert_matching_weights(
    body: MatchingWeightsUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[MatchingWeightsRead]:
    data = await matching_weights_service.upsert(session, body, principal.id)
    return ResponseEnvelope[MatchingWeightsRead](data=data, meta=Meta(request_id=request_id))


@router.get("/engine-settings", response_model=ResponseEnvelope[EngineSettingsRead])
async def get_engine_settings(
    session: SessionDep,
    request_id: RequestIdDep,
    country: str | None = None,
    visa_type: OptionalVisaType = None,
) -> ResponseEnvelope[EngineSettingsRead]:
    """Dashboard bootstrap: thresholds for scope + global matching weights."""
    threshold = await assessment_service.get_threshold(session, country, visa_type)
    weights = await matching_weights_service.get_read(session)
    return ResponseEnvelope[EngineSettingsRead](
        data=EngineSettingsRead(
            thresholds=_threshold_read(threshold) if threshold else None,
            matching_weights=weights,
        ),
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/ab-variants",
    status_code=201,
    response_model=ResponseEnvelope[AbVariantRead],
)
async def create_ab_variant(
    body: AbVariantCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AbVariantRead]:
    row = await ab_variant_service.create(session, body, principal.id)
    data = (await ab_variant_service.build_reads(session, [row]))[0]
    return ResponseEnvelope[AbVariantRead](data=data, meta=Meta(request_id=request_id))


@router.get("/ab-variants", response_model=ResponseEnvelope[list[AbVariantRead]])
async def list_ab_variants(
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    country: str | None = None,
    visa_type: OptionalVisaType = None,
) -> ResponseEnvelope[list[AbVariantRead]]:
    stmt = ab_variant_service.list_stmt(country, visa_type)
    rows, total = await paginate(session, stmt, params)
    data = await ab_variant_service.build_reads(session, rows)
    return ResponseEnvelope[list[AbVariantRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.patch(
    "/ab-variants/{variant_id}",
    response_model=ResponseEnvelope[AbVariantRead],
)
async def update_ab_variant(
    variant_id: uuid.UUID,
    body: AbVariantUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AbVariantRead]:
    row = await ab_variant_service.get_by_id(session, variant_id)
    row = await ab_variant_service.update(session, row, body, principal.id)
    data = (await ab_variant_service.build_reads(session, [row]))[0]
    return ResponseEnvelope[AbVariantRead](data=data, meta=Meta(request_id=request_id))


@router.delete("/ab-variants/{variant_id}", status_code=204)
async def delete_ab_variant(
    variant_id: uuid.UUID,
    session: SessionDep,
) -> None:
    row = await ab_variant_service.get_by_id(session, variant_id)
    await ab_variant_service.delete(session, row)


# ── Review moderation (PRD §3.9) ─────────────────────────────────────────────


def _review_admin_read(review: Review, seeker: User | None) -> ReviewAdminRead:
    base = review_service.build_read(review, seeker)
    return ReviewAdminRead(
        **base.model_dump(),
        seeker_id=review.seeker_id,
        moderation_status=review.moderation_status,
        flag_reason=review.flag_reason,
    )


@router.get("/reviews/flagged", response_model=ResponseEnvelope[list[ReviewAdminRead]])
async def list_flagged_reviews(
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[ReviewAdminRead]]:
    """Moderation queue: reviews reported by users, awaiting a decision."""
    stmt = review_service.list_flagged_stmt()
    reviews, total = await paginate(session, stmt, params)
    data = []
    for review in reviews:
        seeker = await session.get(User, review.seeker_id)
        data.append(_review_admin_read(review, seeker))
    return ResponseEnvelope[list[ReviewAdminRead]](
        data=data,
        meta=page_meta(params, total, request_id),
    )


@router.patch(
    "/reviews/{review_id}/moderation",
    response_model=ResponseEnvelope[ReviewAdminRead],
)
async def moderate_review(
    review_id: uuid.UUID,
    body: ModerationDecision,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ReviewAdminRead]:
    """Approve (restore to visible) or remove a flagged review."""
    review = await review_service.get_by_id(session, review_id)
    review = await review_service.moderate(session, review, body.action, principal.id)
    seeker = await session.get(User, review.seeker_id)
    return ResponseEnvelope[ReviewAdminRead](
        data=_review_admin_read(review, seeker),
        meta=Meta(request_id=request_id),
    )


# ── Finance management (PRD §4.5) ────────────────────────────────────────────


@router.get("/payments/summary", response_model=ResponseEnvelope[PaymentSummaryRead])
async def get_payments_summary(
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[PaymentSummaryRead]:
    """Platform-wide payment summary cards (paid / refunded / commission / tax)."""
    data = await payment_service.platform_payment_summary(session)
    return ResponseEnvelope[PaymentSummaryRead](data=data, meta=Meta(request_id=request_id))


@router.get("/payments", response_model=ResponseEnvelope[list[TransactionFinanceRead]])
async def list_all_payments(
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    status: TransactionStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    search: str | None = None,
) -> ResponseEnvelope[list[TransactionFinanceRead]]:
    """Full transaction list across the platform, optionally filtered."""
    stmt = payment_service.list_all_stmt(status, date_from, date_to, search)
    txns, total = await paginate(session, stmt, params)
    data = [await payment_service.finance_read(session, t) for t in txns]
    return ResponseEnvelope[list[TransactionFinanceRead]](
        data=data,
        meta=page_meta(params, total, request_id),
    )


@router.get("/payments/{transaction_id}", response_model=ResponseEnvelope[TransactionFinanceRead])
async def get_payment(
    transaction_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TransactionFinanceRead]:
    """Transaction Information modal — full detail with customer/advisor parties."""
    txn = await payment_service.get_by_id(session, transaction_id)
    data = await payment_service.finance_read(session, txn)
    return ResponseEnvelope[TransactionFinanceRead](
        data=data,
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/payments/{transaction_id}/invoice",
    response_model=ResponseEnvelope[InvoiceRead],
)
async def get_admin_payment_invoice(
    transaction_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[InvoiceRead]:
    """Admin invoice download — platform perspective."""
    txn = await payment_service.get_by_id(session, transaction_id)
    invoice = await payment_service.build_invoice(
        session, txn, settings, perspective="admin"
    )
    return ResponseEnvelope[InvoiceRead](data=invoice, meta=Meta(request_id=request_id))


@router.post(
    "/payments/{transaction_id}/send-email",
    response_model=ResponseEnvelope[dict[str, bool]],
)
async def send_payment_receipt(
    transaction_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[dict[str, bool]]:
    """Resend the payment receipt email to the seeker."""
    txn = await payment_service.get_by_id(session, transaction_id)
    await payment_service.resend_receipt(session, txn, settings)
    return ResponseEnvelope[dict[str, bool]](
        data={"sent": True}, meta=Meta(request_id=request_id)
    )


@router.get(
    "/payments/{transaction_id}/timeline",
    response_model=ResponseEnvelope[list[TransactionEventRead]],
)
async def get_payment_timeline(
    transaction_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[TransactionEventRead]]:
    """Timeline & Logs modal — ordered lifecycle events for a transaction."""
    await payment_service.get_by_id(session, transaction_id)  # 404s if missing
    events = await payment_service.list_events(session, transaction_id)
    return ResponseEnvelope[list[TransactionEventRead]](
        data=[TransactionEventRead.model_validate(e) for e in events],
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/payments/{transaction_id}/refund",
    response_model=ResponseEnvelope[TransactionFinanceRead],
)
async def refund_payment(
    transaction_id: uuid.UUID,
    body: RefundCreate,
    principal: CurrentPrincipal,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TransactionFinanceRead]:
    """Issue a full or partial refund for a completed payment."""
    txn = await payment_service.refund_transaction(
        session, transaction_id, principal.id, body.reason, settings, body.amount_usd
    )
    data = await payment_service.finance_read(session, txn)
    return ResponseEnvelope[TransactionFinanceRead](
        data=data,
        meta=Meta(request_id=request_id),
    )


@router.get("/payouts", response_model=ResponseEnvelope[list[PayoutRequestRead]])
async def list_all_payouts(
    params: PaginationDep,
    session: SessionDep,
    request_id: RequestIdDep,
    status: PayoutStatus | None = None,
) -> ResponseEnvelope[list[PayoutRequestRead]]:
    """Full payout request queue across the platform, optionally filtered by status."""
    stmt = payout_service.list_all_stmt(status)
    payouts, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[PayoutRequestRead]](
        data=[PayoutRequestRead.model_validate(p) for p in payouts],
        meta=page_meta(params, total, request_id),
    )


@router.patch(
    "/payouts/{payout_id}",
    response_model=ResponseEnvelope[PayoutRequestRead],
)
async def review_payout(
    payout_id: uuid.UUID,
    body: PayoutDecision,
    principal: CurrentPrincipal,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[PayoutRequestRead]:
    """Complete or reject a pending payout request."""
    payout = await payout_service.get_by_id(session, payout_id)
    if body.action == PayoutStatus.completed:
        payout = await payout_service.complete(session, payout, principal.id)
    elif body.action == PayoutStatus.rejected:
        payout = await payout_service.reject(session, payout, principal.id, body.rejection_reason)
    else:
        from app.core.exceptions import AppError

        raise AppError("action must be 'completed' or 'rejected'", code="invalid_action")
    return ResponseEnvelope[PayoutRequestRead](
        data=PayoutRequestRead.model_validate(payout),
        meta=Meta(request_id=request_id),
    )


@router.get("/messages/flagged", response_model=ResponseEnvelope[list[FlaggedMessageRead]])
async def list_flagged_messages(
    params: PaginationDep,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[FlaggedMessageRead]]:
    """Moderation queue: messages reported by users, awaiting a decision."""
    stmt = conversation_service.list_flagged_stmt()
    messages, total = await paginate(session, stmt, params)
    data = []
    for message in messages:
        sender = await session.get(User, message.sender_id)
        data.append(conversation_service.build_flagged_read(message, sender, settings))
    return ResponseEnvelope[list[FlaggedMessageRead]](
        data=data,
        meta=page_meta(params, total, request_id),
    )


@router.patch(
    "/messages/{message_id}/moderation",
    response_model=ResponseEnvelope[FlaggedMessageRead],
)
async def moderate_message(
    message_id: uuid.UUID,
    body: ModerationDecision,
    principal: CurrentPrincipal,
    settings: SettingsDep,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[FlaggedMessageRead]:
    """Approve (restore to visible) or remove a flagged message."""
    message = await conversation_service.get_by_id(session, message_id)
    message = await conversation_service.moderate(session, message, body.action, principal.id)
    sender = await session.get(User, message.sender_id)
    return ResponseEnvelope[FlaggedMessageRead](
        data=conversation_service.build_flagged_read(message, sender, settings),
        meta=Meta(request_id=request_id),
    )


# ── Seeker document review (PRD §3.8, admin access alongside assigned advisor) ──


@router.get(
    "/seekers/{seeker_id}/documents",
    response_model=ResponseEnvelope[list[SeekerDocumentRead]],
)
async def list_seeker_documents_admin(
    seeker_id: uuid.UUID,
    params: PaginationDep,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[SeekerDocumentRead]]:
    stmt = seeker_document_service.list_by_seeker_stmt(seeker_id)
    documents, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[SeekerDocumentRead]](
        data=[seeker_document_service.build_read(d, settings) for d in documents],
        meta=page_meta(params, total, request_id),
    )


@router.patch(
    "/seekers/{seeker_id}/documents/{document_id}",
    response_model=ResponseEnvelope[SeekerDocumentRead],
)
async def review_seeker_document_admin(
    seeker_id: uuid.UUID,
    document_id: uuid.UUID,
    body: SeekerDocumentStatusUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerDocumentRead]:
    document = await seeker_document_service.get_for_seeker(session, document_id, seeker_id)
    document = await seeker_document_service.set_status(session, document, body, principal.id)
    return ResponseEnvelope[SeekerDocumentRead](
        data=seeker_document_service.build_read(document, settings),
        meta=Meta(request_id=request_id),
    )


# ── Support tickets (PRD §4.6 Support & Moderation) ──────────────────────────


@router.post("/support-tickets", status_code=201, response_model=ResponseEnvelope[TicketRead])
async def create_support_ticket(
    body: TicketCreate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TicketRead]:
    """Create a ticket.

    Body accepts ``user_id`` **or** ``user_email``, optional ``assigned_to``,
    ``internal_notes``, and ``attachments[]`` (upload via ``POST /uploads`` with
    ``category=ticket_attachment`` first). Category alias: ``payment`` → ``billing``.
    """
    from app.core.exceptions import PermissionDeniedError

    opening: list[TicketMessageAttachment] = []
    expected_prefix = f"ticket_attachment/{principal.id}/"
    for ref in body.attachments:
        if not ref.file_key.startswith(expected_prefix):
            raise PermissionDeniedError("Invalid attachment key")
        opening.append(
            TicketMessageAttachment(
                file_url=resolve_url(f"/uploads/{ref.file_key}", settings),
                file_name=ref.file_name,
                file_size=ref.file_size_bytes,
                content_type=ref.content_type,
            )
        )

    ticket = await support_ticket_service.create(
        session, body, principal.id, opening_attachments=opening
    )
    return ResponseEnvelope[TicketRead](
        data=await support_ticket_service.ticket_read(session, ticket, settings),
        meta=Meta(request_id=request_id),
    )


@router.get("/support-tickets", response_model=ResponseEnvelope[list[TicketRead]])
async def list_support_tickets(
    params: PaginationDep,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    status: str | None = None,
    priority: TicketPriority | None = None,
    category: str | None = None,
    search: str | None = None,
) -> ResponseEnvelope[list[TicketRead]]:
    """List support tickets.

    ``status`` accepts canonical values (``open``, ``in_progress``, ``resolved``,
    ``closed``) plus FE aliases ``pending``→open, ``inprogress``/``escalated``→
    in_progress. ``category`` accepts ``payment`` as an alias for ``billing``.
    Conversation thread: ``GET/POST .../{id}/messages``.
    """
    from app.core.exceptions import AppError

    try:
        status_filter = support_ticket_service.coerce_status_filter(status)
    except ValueError as exc:
        raise AppError(str(exc), code="invalid_status") from exc
    try:
        category_filter = support_ticket_service.coerce_category_filter(category)
    except ValueError as exc:
        raise AppError(str(exc), code="invalid_category") from exc

    stmt = support_ticket_service.list_stmt(status_filter, priority, category_filter, search)
    tickets, total = await paginate(session, stmt, params)
    data = await support_ticket_service.build_list_reads(session, tickets, settings)
    return ResponseEnvelope[list[TicketRead]](data=data, meta=page_meta(params, total, request_id))


@router.get("/support-tickets/{ticket_id}", response_model=ResponseEnvelope[TicketRead])
async def get_support_ticket(
    ticket_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TicketRead]:
    """Ticket detail. Messages: ``GET /support-tickets/{id}/messages``."""
    ticket = await support_ticket_service.get_by_id(session, ticket_id)
    return ResponseEnvelope[TicketRead](
        data=await support_ticket_service.ticket_read(session, ticket, settings),
        meta=Meta(request_id=request_id),
    )


@router.patch("/support-tickets/{ticket_id}", response_model=ResponseEnvelope[TicketRead])
async def update_support_ticket(
    ticket_id: uuid.UUID,
    body: TicketUpdate,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TicketRead]:
    ticket = await support_ticket_service.get_by_id(session, ticket_id)
    ticket = await support_ticket_service.update(session, ticket, body, principal.id)
    return ResponseEnvelope[TicketRead](
        data=await support_ticket_service.ticket_read(session, ticket, settings),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/support-tickets/{ticket_id}/messages",
    response_model=ResponseEnvelope[list[TicketMessageRead]],
)
async def list_support_ticket_messages(
    ticket_id: uuid.UUID,
    params: PaginationDep,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[TicketMessageRead]]:
    await support_ticket_service.get_by_id(session, ticket_id)  # 404s if missing
    stmt = ticket_message_service.list_messages_stmt(ticket_id)
    messages, total = await paginate(session, stmt, params)
    senders = {m.sender_id: await session.get(User, m.sender_id) for m in messages}
    data = [
        ticket_message_service.build_message_read(m, senders.get(m.sender_id), settings)
        for m in messages
    ]
    return ResponseEnvelope[list[TicketMessageRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.post(
    "/support-tickets/{ticket_id}/messages",
    status_code=201,
    response_model=ResponseEnvelope[TicketMessageRead],
)
async def send_support_ticket_message(
    ticket_id: uuid.UUID,
    body: TicketMessageSend,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TicketMessageRead]:
    from app.core.exceptions import NotFoundError, PermissionDeniedError

    ticket = await support_ticket_service.get_by_id(session, ticket_id)
    admin_user = await session.get(User, principal.id)
    if admin_user is None:
        raise NotFoundError("User not found")

    attachments: list[TicketMessageAttachment] = []
    expected_prefix = f"ticket_attachment/{principal.id}/"
    for ref in body.attachments:
        if not ref.file_key.startswith(expected_prefix):
            raise PermissionDeniedError("Invalid attachment key")
        attachments.append(
            TicketMessageAttachment(
                file_url=resolve_url(f"/uploads/{ref.file_key}", settings),
                file_name=ref.file_name,
                file_size=ref.file_size_bytes,
                content_type=ref.content_type,
            )
        )

    message = await ticket_message_service.send_message(
        session, ticket, admin_user, body.body, attachments
    )
    return ResponseEnvelope[TicketMessageRead](
        data=ticket_message_service.build_message_read(message, admin_user, settings),
        meta=Meta(request_id=request_id),
    )


# ── Analytics dashboard ──────────────────────────────────────────────────────


@router.get("/analytics/overview", response_model=ResponseEnvelope[OverviewAnalyticsRead])
async def get_overview_analytics(
    session: SessionDep, request_id: RequestIdDep, days: int = 30
) -> ResponseEnvelope[OverviewAnalyticsRead]:
    data = await analytics_service.get_overview_analytics(session, days)
    return ResponseEnvelope[OverviewAnalyticsRead](data=data, meta=Meta(request_id=request_id))


@router.get("/analytics/advisors", response_model=ResponseEnvelope[AdvisorAnalyticsRead])
async def get_advisor_analytics(
    session: SessionDep, request_id: RequestIdDep, days: int = 30
) -> ResponseEnvelope[AdvisorAnalyticsRead]:
    data = await analytics_service.get_advisor_analytics(session, days)
    return ResponseEnvelope[AdvisorAnalyticsRead](data=data, meta=Meta(request_id=request_id))


@router.get("/analytics/finance", response_model=ResponseEnvelope[FinanceAnalyticsRead])
async def get_finance_analytics(
    session: SessionDep, request_id: RequestIdDep, days: int = 30
) -> ResponseEnvelope[FinanceAnalyticsRead]:
    data = await analytics_service.get_finance_analytics(session, days)
    return ResponseEnvelope[FinanceAnalyticsRead](data=data, meta=Meta(request_id=request_id))


@router.get("/analytics/ai", response_model=ResponseEnvelope[AIAnalyticsRead])
async def get_ai_analytics(
    session: SessionDep, request_id: RequestIdDep, days: int = 270
) -> ResponseEnvelope[AIAnalyticsRead]:
    """Default window ~9 months so seeded 8-month volume appears on the panel."""
    data = await analytics_service.get_ai_analytics(session, days)
    return ResponseEnvelope[AIAnalyticsRead](data=data, meta=Meta(request_id=request_id))


@router.get("/analytics/engagement", response_model=ResponseEnvelope[EngagementAnalyticsRead])
async def get_engagement_analytics(
    session: SessionDep, request_id: RequestIdDep, days: int = 30
) -> ResponseEnvelope[EngagementAnalyticsRead]:
    data = await analytics_service.get_engagement_analytics(session, days)
    return ResponseEnvelope[EngagementAnalyticsRead](data=data, meta=Meta(request_id=request_id))


# ── Dashboard home screen ────────────────────────────────────────────────────


@router.get("/dashboard", response_model=ResponseEnvelope[DashboardSummaryRead])
async def get_dashboard(
    session: SessionDep, request_id: RequestIdDep, days: int = 180
) -> ResponseEnvelope[DashboardSummaryRead]:
    data = await dashboard_service.get_dashboard_summary(session, days)
    return ResponseEnvelope[DashboardSummaryRead](data=data, meta=Meta(request_id=request_id))


@router.get("/activities", response_model=ResponseEnvelope[list[ActivityFeedItemRead]])
async def list_activities(
    session: SessionDep, request_id: RequestIdDep, params: PaginationDep, days: int = 180
) -> ResponseEnvelope[list[ActivityFeedItemRead]]:
    items, total = await dashboard_service.list_recent_activities_page(session, days, params)
    return ResponseEnvelope[list[ActivityFeedItemRead]](
        data=items, meta=page_meta(params, total, request_id)
    )
