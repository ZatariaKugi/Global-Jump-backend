"""Admin "Advisor Management" — enriched list/detail plus the 4 detail-page
tabs (Overview, Session History, Earnings, Reviews)."""

from __future__ import annotations

import uuid
from typing import Literal, cast

from sqlalchemy import Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.countries import country_name
from app.core.exceptions import NotFoundError
from app.core.file_storage import resolve_media_url
from app.models.advisor_credential import AdvisorCredential, CredentialStatus
from app.models.advisor_profile import AdvisorProfile, AdvisorVisaSpecialization
from app.models.booking import Booking, BookingStatus
from app.models.payout_request import PayoutRequest, PayoutStatus
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole, VerificationStatus
from app.models.visa_type import VisaType
from app.schemas.advisor_admin import (
    AdvisorEarningRowRead,
    AdvisorEarningsSummaryRead,
    AdvisorManagementDetailRead,
    AdvisorManagementListRead,
    AdvisorSessionRead,
    AdvisorStatus,
)
from app.schemas.advisor_profile import LanguageEntry
from app.services import (
    advisor_profile_service,
    booking_service,
    payment_service,
    payout_service,
    review_service,
    user_admin_service,
)

_LanguageProficiency = Literal["basic", "conversational", "fluent", "native"]


def advisor_display_status(user: User) -> AdvisorStatus:
    """UI badge from ``verification_status`` (frontend key), with ``is_suspended`` fallback."""
    if getattr(user, "is_suspended", False) or (
        user.verification_status == VerificationStatus.suspended
    ):
        return AdvisorStatus.suspended
    if user.verification_status is None:
        return AdvisorStatus.pending
    return AdvisorStatus(user.verification_status.value)


def list_advisors_stmt(
    search: str | None,
    status: VerificationStatus | None,
    visa_type: VisaType | None = None,
) -> Select[tuple[User]]:
    """Same shape as seeker_admin_service.list_seekers_stmt, scoped to advisors.

    ``visa_type`` filters against ``advisor_visa_specializations`` (PRD enum only).
    """
    stmt = select(User).where(User.role == UserRole.advisor).order_by(User.created_at.desc())
    if status is not None:
        stmt = stmt.where(User.verification_status == status)
    clause = user_admin_service.user_search_clause(search)
    if clause is not None:
        stmt = stmt.where(clause)
    if visa_type is not None:
        stmt = stmt.where(
            exists().where(
                AdvisorProfile.user_id == User.id,
                AdvisorVisaSpecialization.profile_id == AdvisorProfile.id,
                func.lower(AdvisorVisaSpecialization.specialization) == visa_type.value,
            )
        )
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
    session: AsyncSession, advisors: list[User], settings: Settings
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

    earnings_rows = (
        await session.execute(
            select(
                Booking.advisor_id,
                func.coalesce(func.sum(Transaction.advisor_payout_usd), 0.0),
            )
            .join(Transaction, Transaction.booking_id == Booking.id)
            .where(
                Booking.advisor_id.in_(ids),
                Transaction.status == TransactionStatus.succeeded,
            )
            .group_by(Booking.advisor_id)
        )
    ).all()
    earnings_by_advisor: dict[uuid.UUID, float] = {}
    for advisor_id, total in earnings_rows:
        earnings_by_advisor[advisor_id] = round(float(total), 2)

    out = []
    for a in advisors:
        profile = profiles.get(a.id)
        avg, review_count = ratings.get(a.id, (None, 0))
        residence_code = profile.country_of_residence if profile else None
        out.append(
            AdvisorManagementListRead(
                id=a.id,
                full_name=a.full_name,
                email=a.email,
                profile_photo_url=(
                    resolve_media_url(profile.profile_photo_url, settings) if profile else None
                ),
                country_code=residence_code,
                country=country_name(residence_code),
                expertise=(
                    [s.specialization for s in profile.visa_specializations] if profile else []
                ),
                status=advisor_display_status(a),
                verification_status=a.verification_status,
                is_suspended=bool(getattr(a, "is_suspended", False)),
                is_active=a.is_active,
                session_count=session_counts.get(a.id, 0),
                avg_rating=avg,
                review_count=review_count,
                earnings=earnings_by_advisor.get(a.id, 0.0),
                created_at=a.created_at,
            )
        )
    return out


async def get_advisor_detail(
    session: AsyncSession, advisor_id: uuid.UUID, settings: Settings
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

    earnings = await payment_service.get_advisor_earnings(session, advisor_id)
    total_earned_usd = float(earnings["total_earned_usd"])  # type: ignore[arg-type]

    expertise_codes = [c.country_code for c in profile.country_expertise]
    return AdvisorManagementDetailRead(
        id=advisor.id,
        full_name=advisor.full_name,
        email=advisor.email,
        profile_photo_url=resolve_media_url(profile.profile_photo_url, settings),
        country_code=profile.country_of_residence,
        country=country_name(profile.country_of_residence),
        expertise=[s.specialization for s in profile.visa_specializations],
        status=advisor_display_status(advisor),
        verification_status=advisor.verification_status,
        is_suspended=bool(getattr(advisor, "is_suspended", False)),
        is_active=advisor.is_active,
        session_count=total_sessions,
        avg_rating=avg,
        review_count=review_count,
        earnings=total_earned_usd,
        created_at=advisor.created_at,
        title=profile.title,
        bio=profile.bio,
        years_of_experience=profile.years_of_experience,
        successful_applications=profile.successful_applications,
        successful_application_rate=profile.successful_application_rate,
        country_expertise=expertise_codes,
        country_expertise_names=[
            country_name(code) or code for code in expertise_codes
        ],
        languages=[
            LanguageEntry(
                language=lang.language,
                proficiency=cast(_LanguageProficiency, lang.proficiency),
            )
            for lang in profile.languages
        ],
        completed_sessions=completed_sessions,
        credentials_pending_count=cred_counts.get(CredentialStatus.pending, 0),
        credentials_verified_count=cred_counts.get(CredentialStatus.verified, 0),
    )


async def build_session_reads(
    session: AsyncSession, bookings: list[Booking], settings: Settings
) -> list[AdvisorSessionRead]:
    """Session History tab — bulk seeker name + consultation counts (not N+1)."""
    if not bookings:
        return []

    seeker_ids = list({b.seeker_id for b in bookings})
    advisor_id = bookings[0].advisor_id

    seekers: dict[uuid.UUID, User] = {}
    rows = (await session.execute(select(User).where(User.id.in_(seeker_ids)))).scalars().all()
    seekers = {u.id: u for u in rows}

    advisor = await session.get(User, advisor_id)
    photos = await booking_service.advisor_photo_keys(session, {advisor_id})
    photo_key = photos.get(advisor_id)
    seeker_photos = await booking_service.seeker_photo_keys(session, set(seeker_ids))

    count_rows = (
        await session.execute(
            select(Booking.seeker_id, func.count())
            .where(
                Booking.advisor_id == advisor_id,
                Booking.seeker_id.in_(seeker_ids),
            )
            .group_by(Booking.seeker_id)
        )
    ).all()
    consultation_counts: dict[uuid.UUID, int] = {}
    for seeker_id, count in count_rows:
        consultation_counts[seeker_id] = count

    # Seeded country values for Session History UI until seeker profiles are populated.
    _SEED_COUNTRY_CODE = "GB"
    _SEED_COUNTRY_NAME = "United Kingdom"

    out: list[AdvisorSessionRead] = []
    for b in bookings:
        base = booking_service.build_read(
            b,
            seekers.get(b.seeker_id),
            advisor,
            settings=settings,
            advisor_profile_photo_key=photo_key,
            seeker_profile_photo_key=seeker_photos.get(b.seeker_id),
        )
        out.append(
            AdvisorSessionRead(
                **base.model_dump(),
                country_code=_SEED_COUNTRY_CODE,
                country_name=_SEED_COUNTRY_NAME,
                consultation_count=consultation_counts.get(b.seeker_id, 0),
            )
        )
    return out


async def get_earnings_summary(
    session: AsyncSession, advisor_id: uuid.UUID
) -> AdvisorEarningsSummaryRead:
    advisor = await session.get(User, advisor_id)
    if advisor is None or advisor.role != UserRole.advisor:
        raise NotFoundError("Advisor not found")

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

    total_earned_usd = float(earnings["total_earned_usd"])  # type: ignore[arg-type]
    total_commission_paid_usd = float(earnings["total_commission_paid_usd"])  # type: ignore[arg-type]
    transactions = earnings["transactions"]
    assert isinstance(transactions, list)
    items = await _build_earning_rows(session, cast(list[Transaction], transactions))
    return AdvisorEarningsSummaryRead(
        total_earned_usd=total_earned_usd,
        total_commission_paid_usd=total_commission_paid_usd,
        available_balance_usd=available,
        total_payouts_usd=round(payout_totals.get(PayoutStatus.completed, 0.0), 2),
        pending_payout_usd=round(payout_totals.get(PayoutStatus.pending, 0.0), 2),
        transaction_count=len(items),
        items=items,
    )


async def _build_earning_rows(
    session: AsyncSession, transactions: list[Transaction]
) -> list[AdvisorEarningRowRead]:
    """Bulk-enrich transactions into Earnings-tab table rows (not N+1)."""
    if not transactions:
        return []

    booking_ids = [t.booking_id for t in transactions]
    bookings = (
        (await session.execute(select(Booking).where(Booking.id.in_(booking_ids))))
        .scalars()
        .all()
    )
    booking_by_id = {b.id: b for b in bookings}

    seeker_ids = list({b.seeker_id for b in bookings})
    seekers: dict[uuid.UUID, User] = {}
    photos: dict[uuid.UUID, str | None] = {}
    if seeker_ids:
        seeker_rows = (
            (await session.execute(select(User).where(User.id.in_(seeker_ids)))).scalars().all()
        )
        seekers = {u.id: u for u in seeker_rows}
        profile_rows = (
            (
                await session.execute(
                    select(SeekerProfile).where(SeekerProfile.user_id.in_(seeker_ids))
                )
            )
            .scalars()
            .all()
        )
        photos = {p.user_id: p.profile_photo_url for p in profile_rows}

    rows: list[AdvisorEarningRowRead] = []
    for txn in transactions:
        booking = booking_by_id.get(txn.booking_id)
        if booking is None:
            continue
        seeker = seekers.get(booking.seeker_id)
        rows.append(
            AdvisorEarningRowRead(
                appointment_id=payment_service.format_appointment_id(booking.appointment_number),
                booking_id=txn.booking_id,
                seeker_name=seeker.full_name if seeker else None,
                seeker_email=seeker.email if seeker else None,
                seeker_photo_url=photos.get(booking.seeker_id),
                created_at=txn.created_at,
                amount_paid=round(float(txn.amount_usd), 2),
                platform_fee=round(float(txn.commission_usd), 2),
                advisor_earnings=round(float(txn.advisor_payout_usd), 2),
                status=payment_service.display_status(txn),
            )
        )
    return rows
