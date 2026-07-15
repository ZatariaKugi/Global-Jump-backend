"""Admin "Advisor Management" — enriched list/detail plus the 4 detail-page
tabs (Overview, Session History, Earnings, Reviews)."""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.countries import country_name
from app.core.exceptions import NotFoundError
from app.models.advisor_credential import AdvisorCredential, CredentialStatus
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking, BookingStatus
from app.models.payout_request import PayoutRequest, PayoutStatus
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.advisor_admin import (
    AdvisorEarningsSummaryRead,
    AdvisorManagementDetailRead,
    AdvisorManagementListRead,
)
from app.schemas.advisor_profile import LanguageEntry
from app.schemas.booking import BookingRead
from app.services import advisor_profile_service, payment_service, payout_service, review_service


def list_advisors_stmt(
    search: str | None, status: VerificationStatus | None
) -> Select[tuple[User]]:
    """Same shape as seeker_admin_service.list_seekers_stmt, scoped to advisors."""
    stmt = select(User).where(User.role == UserRole.advisor).order_by(User.created_at.desc())
    if status is not None:
        stmt = stmt.where(User.verification_status == status)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(pattern), User.email.ilike(pattern)))
    return stmt


async def _profiles_by_user(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, AdvisorProfile]:
    if not user_ids:
        return {}
    rows = (
        (await session.execute(select(AdvisorProfile).where(AdvisorProfile.user_id.in_(user_ids))))
        .scalars()
        .all()
    )
    return {p.user_id: p for p in rows}


async def build_list_read(
    session: AsyncSession, advisors: list[User]
) -> list[AdvisorManagementListRead]:
    """Bulk-enrich one page: profiles + Sessions count + Rating, each a
    single grouped query keyed by the page's user ids — not N+1."""
    ids = [a.id for a in advisors]
    if not ids:
        return []
    profiles = await _profiles_by_user(session, ids)

    session_rows = (
        await session.execute(
            select(Booking.advisor_id, func.count())
            .where(Booking.advisor_id.in_(ids))
            .group_by(Booking.advisor_id)
        )
    ).all()
    session_counts: dict[uuid.UUID, int] = {}
    for advisor_id, count in session_rows:
        session_counts[advisor_id] = count

    ratings = await review_service.rating_summaries(session, ids)

    out = []
    for a in advisors:
        profile = profiles.get(a.id)
        avg, review_count = ratings.get(a.id, (None, 0))
        out.append(
            AdvisorManagementListRead(
                id=a.id,
                full_name=a.full_name,
                email=a.email,
                profile_photo_url=profile.profile_photo_url if profile else None,
                country_code=(
                    profile.country_expertise[0].country_code
                    if profile and profile.country_expertise
                    else None
                ),
                country=(
                    country_name(profile.country_expertise[0].country_code)
                    if profile and profile.country_expertise
                    else None
                ),
                expertise=(
                    [s.specialization for s in profile.visa_specializations] if profile else []
                ),
                verification_status=a.verification_status,
                is_active=a.is_active,
                session_count=session_counts.get(a.id, 0),
                avg_rating=avg,
                review_count=review_count,
                created_at=a.created_at,
            )
        )
    return out


async def get_advisor_detail(
    session: AsyncSession, advisor_id: uuid.UUID
) -> AdvisorManagementDetailRead:
    advisor = await session.get(User, advisor_id)
    if advisor is None or advisor.role != UserRole.advisor:
        raise NotFoundError("Advisor not found")
    profile = await advisor_profile_service.get_or_create(session, advisor_id)

    total_sessions = (
        await session.execute(
            select(func.count()).select_from(Booking).where(Booking.advisor_id == advisor_id)
        )
    ).scalar_one()
    completed_sessions = (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(Booking.advisor_id == advisor_id, Booking.status == BookingStatus.completed)
        )
    ).scalar_one()
    avg, review_count = await review_service.rating_summary(session, advisor_id)

    cred_rows = (
        await session.execute(
            select(AdvisorCredential.status, func.count())
            .where(AdvisorCredential.user_id == advisor_id)
            .group_by(AdvisorCredential.status)
        )
    ).all()
    cred_counts: dict[CredentialStatus, int] = {}
    for status, count in cred_rows:
        cred_counts[status] = count

    expertise_codes = [c.country_code for c in profile.country_expertise]
    residence_code = expertise_codes[0] if expertise_codes else None
    return AdvisorManagementDetailRead(
        id=advisor.id,
        full_name=advisor.full_name,
        email=advisor.email,
        profile_photo_url=profile.profile_photo_url,
        country_code=residence_code,
        country=country_name(residence_code),
        expertise=[s.specialization for s in profile.visa_specializations],
        verification_status=advisor.verification_status,
        is_active=advisor.is_active,
        session_count=total_sessions,
        avg_rating=avg,
        review_count=review_count,
        created_at=advisor.created_at,
        title=profile.title,
        bio=profile.bio,
        years_of_experience=profile.years_of_experience,
        successful_applications=profile.successful_applications,
        successful_application_rate=profile.successful_application_rate,
        country_expertise=expertise_codes,
        country_expertise_names=[country_name(code) or code for code in expertise_codes],
        languages=[
            LanguageEntry(language=lang.language, proficiency=lang.proficiency)
            for lang in profile.languages
        ],
        completed_sessions=completed_sessions,
        credentials_pending_count=cred_counts.get(CredentialStatus.pending, 0),
        credentials_verified_count=cred_counts.get(CredentialStatus.verified, 0),
    )


async def build_session_reads(session: AsyncSession, bookings: list[Booking]) -> list[BookingRead]:
    """Session History tab — bulk seeker-name enrichment (not N+1)."""
    seeker_ids = [b.seeker_id for b in bookings]
    seekers: dict[uuid.UUID, User] = {}
    if seeker_ids:
        rows = (await session.execute(select(User).where(User.id.in_(seeker_ids)))).scalars().all()
        seekers = {u.id: u for u in rows}
    return [
        BookingRead(
            id=b.id,
            seeker_id=b.seeker_id,
            advisor_id=b.advisor_id,
            seeker_name=seekers[b.seeker_id].full_name if b.seeker_id in seekers else None,
            advisor_name=None,
            service_type=b.service_type,
            duration_minutes=b.duration_minutes,
            price_usd=b.price_usd,
            scheduled_start=b.scheduled_start,
            scheduled_end=b.scheduled_end,
            status=b.status,
            payment_status=b.payment_status,
            cancellation_reason=b.cancellation_reason,
            seeker_note=b.seeker_note,
            is_important=b.is_important,
            interpreter_name=b.interpreter_name,
            interpreter_contact=b.interpreter_contact,
            interpreter_language=b.interpreter_language,
            created_at=b.created_at,
        )
        for b in bookings
    ]


async def get_earnings_summary(
    session: AsyncSession, advisor_id: uuid.UUID
) -> AdvisorEarningsSummaryRead:
    earnings = await payment_service.get_advisor_earnings(session, advisor_id)
    available = await payout_service.get_available_balance(session, advisor_id)

    payout_rows = (
        await session.execute(
            select(PayoutRequest.status, func.coalesce(func.sum(PayoutRequest.amount_usd), 0.0))
            .where(PayoutRequest.advisor_id == advisor_id)
            .group_by(PayoutRequest.status)
        )
    ).all()
    payout_totals: dict[PayoutStatus, float] = {}
    for status, total in payout_rows:
        payout_totals[status] = total

    total_earned_usd = earnings["total_earned_usd"]
    total_commission_paid_usd = earnings["total_commission_paid_usd"]
    transactions = earnings["transactions"]
    assert isinstance(total_earned_usd, float)
    assert isinstance(total_commission_paid_usd, float)
    assert isinstance(transactions, list)
    return AdvisorEarningsSummaryRead(
        total_earned_usd=total_earned_usd,
        total_commission_paid_usd=total_commission_paid_usd,
        available_balance_usd=available,
        total_payouts_usd=round(payout_totals.get(PayoutStatus.completed, 0.0), 2),
        pending_payout_usd=round(payout_totals.get(PayoutStatus.pending, 0.0), 2),
        transaction_count=len(transactions),
    )
