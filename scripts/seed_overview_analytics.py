"""Seed data for the admin Overview analytics User tab.

Populates ``GET /api/v1/admin/analytics/overview`` with:
  - revenue_today_usd (succeeded transactions dated today UTC)
  - users_by_country (map-ready alpha-2 + numeric codes)
  - acquisition_sources (key/label/value pie slices)
  - onboarding_funnel stages
  - retention series (per signup date → day1 / day7 / day30)

Run with::

    uv run python -m scripts.seed_overview_analytics

Idempotent: deletes prior ``overview.seed.*`` users (and cascaded rows), then
recreates. Password for all seed accounts: TestPass123!
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.activity_log import ActivityLog
from app.models.assessment import Assessment, AssessmentStatus, EligibilityTier
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import SignupSource, User, UserRole, VerificationStatus
from app.services import booking_service

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
EMAIL_PREFIX = "overview.seed."
ADVISOR_EMAIL = f"{EMAIL_PREFIX}advisor@globlejump.test"

# Country → how many seekers (map choropleth).
COUNTRY_COUNTS: list[tuple[str, int]] = [
    ("US", 12),
    ("IN", 9),
    ("PK", 7),
    ("GB", 6),
    ("CA", 5),
    ("AE", 4),
    ("NG", 4),
    ("BR", 3),
    ("DE", 3),
    ("AU", 3),
    ("SA", 2),
    ("MX", 2),
]

# Acquisition mix (cycles across seekers).
SOURCES: list[SignupSource] = [
    SignupSource.organic,
    SignupSource.organic,
    SignupSource.paid_ads,
    SignupSource.paid_ads,
    SignupSource.social_media,
    SignupSource.referral_program,
    SignupSource.other,
]

# Today's gross revenue rows (amount_usd).
TODAY_AMOUNTS = (149.0, 199.0, 89.0, 249.0, 129.0)


@dataclass(frozen=True)
class SeekerSpec:
    local: str
    full_name: str
    country: str
    source: SignupSource
    days_ago: int
    # Return days relative to signup (only applied when target ≤ today).
    return_offsets: tuple[int, ...]
    # Funnel: verified always for these seeds; assessment / booking optional.
    with_assessment: bool
    with_booking: bool
    # Include a succeeded payment dated today (needs booking).
    revenue_today: bool = False


def _email(local: str) -> str:
    return f"{EMAIL_PREFIX}{local}@globlejump.test"


def _build_specs() -> list[SeekerSpec]:
    specs: list[SeekerSpec] = []
    n = 0
    for country, count in COUNTRY_COUNTS:
        for _ in range(count):
            n += 1
            # Spread signups across the last 28 days (retention chart window).
            days_ago = 1 + ((n * 3) % 28)
            source = SOURCES[n % len(SOURCES)]
            # Older cohorts get richer return patterns so day7/day30 light up.
            if days_ago >= 30:
                returns: tuple[int, ...] = (1, 7, 30)
            elif days_ago >= 7:
                returns = (1, 7) if n % 3 != 0 else (1,)
            elif days_ago >= 1:
                returns = (1,) if n % 2 == 0 else ()
            else:
                returns = ()

            with_assessment = n % 4 != 0  # ~75%
            with_booking = n % 5 == 0  # ~20% (and implies assessment)
            if with_booking:
                with_assessment = True

            specs.append(
                SeekerSpec(
                    local=f"s{n:02d}",
                    full_name=f"Overview Seeker {n}",
                    country=country,
                    source=source,
                    days_ago=days_ago,
                    return_offsets=returns,
                    with_assessment=with_assessment,
                    with_booking=with_booking,
                )
            )

    # Dedicated payers so revenue_today is stable and obvious.
    for i, _amount in enumerate(TODAY_AMOUNTS, start=1):
        specs.append(
            SeekerSpec(
                local=f"pay{i}",
                full_name=f"Overview Payer {i}",
                country=("US", "GB", "CA", "IN", "AE")[i - 1],
                source=SignupSource.paid_ads,
                days_ago=2 + i,
                return_offsets=(1,),
                with_assessment=True,
                with_booking=True,
                revenue_today=True,
            )
        )
    return specs


async def _clear_prior(session: AsyncSession) -> int:
    users = (
        (await session.execute(select(User).where(User.email.like(f"{EMAIL_PREFIX}%"))))
        .scalars()
        .all()
    )
    if not users:
        return 0
    ids = [u.id for u in users]
    # Bookings reference both seeker and advisor; delete explicitly before users.
    await session.execute(
        delete(Booking).where(Booking.seeker_id.in_(ids) | Booking.advisor_id.in_(ids))
    )
    await session.execute(delete(Assessment).where(Assessment.user_id.in_(ids)))
    await session.execute(delete(ActivityLog).where(ActivityLog.user_id.in_(ids)))
    await session.execute(delete(User).where(User.id.in_(ids)))
    await session.flush()
    return len(ids)


async def _ensure_advisor(session: AsyncSession, password_hash: str) -> User:
    registered_at = datetime.now(UTC) - timedelta(days=40)
    advisor = User(
        email=ADVISOR_EMAIL,
        full_name="Overview Seed Advisor",
        hashed_password=password_hash,
        role=UserRole.advisor,
        is_active=True,
        email_verified_at=registered_at + timedelta(hours=2),
        verification_status=VerificationStatus.approved,
        signup_source=SignupSource.organic,
    )
    advisor.created_at = registered_at
    session.add(advisor)
    await session.flush()
    logger.info("overview_advisor_created", email=ADVISOR_EMAIL)
    return advisor


async def _add_seeker(
    session: AsyncSession, spec: SeekerSpec, password_hash: str
) -> tuple[User, datetime]:
    registered_at = datetime.now(UTC) - timedelta(days=spec.days_ago, hours=spec.days_ago % 12)
    user = User(
        email=_email(spec.local),
        full_name=spec.full_name,
        hashed_password=password_hash,
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=registered_at + timedelta(hours=1),
        verification_status=VerificationStatus.approved,
        signup_source=spec.source,
    )
    user.created_at = registered_at
    session.add(user)
    await session.flush()

    session.add(
        SeekerProfile(
            user_id=user.id,
            nationality=spec.country,
            country_of_residence=spec.country,
            intended_visa_type="work",
            intended_destination="CA",
        )
    )
    await session.flush()
    return user, registered_at


async def _add_activity(
    session: AsyncSession, user: User, signup_day: date, offsets: tuple[int, ...]
) -> None:
    today = datetime.now(UTC).date()
    # Signup-day login.
    session.add(ActivityLog(user_id=user.id, occurred_on=signup_day))
    for n in offsets:
        target = signup_day + timedelta(days=n)
        if target <= today:
            session.add(ActivityLog(user_id=user.id, occurred_on=target))
    await session.flush()


async def _add_assessment(session: AsyncSession, user: User, created_at: datetime) -> None:
    at = created_at + timedelta(hours=3)
    assessment = Assessment(
        user_id=user.id,
        destination_country="CA",
        visa_type="work",
        status=AssessmentStatus.completed,
        score=72.0,
        tier=EligibilityTier.likely_eligible,
        confidence=0.85,
        completed_at=at + timedelta(minutes=12),
        created_by=user.id,
    )
    assessment.created_at = at
    session.add(assessment)
    await session.flush()


async def _add_booking_and_txn(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    scheduled: datetime,
    amount_usd: float,
    txn_at: datetime,
) -> None:
    end = scheduled + timedelta(minutes=30)
    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type="immigration_specialist",
        duration_minutes=30,
        price_usd=amount_usd,
        scheduled_start=scheduled,
        scheduled_end=end,
        status=BookingStatus.completed,
        payment_status=PaymentStatus.paid,
    )
    session.add(booking)
    await session.flush()

    commission_rate = 0.15
    tax_rate = 0.08
    commission_usd = round(amount_usd * commission_rate, 2)
    tax_usd = round(amount_usd * tax_rate, 2)
    advisor_payout_usd = round(amount_usd - commission_usd - tax_usd, 2)
    max_invoice = await session.scalar(select(func.max(Transaction.invoice_number)))
    invoice_number = (max_invoice or 1000) + 1

    txn = Transaction(
        booking_id=booking.id,
        stripe_checkout_session_id=f"cs_overview_{uuid.uuid4().hex[:12]}",
        stripe_payment_intent_id=f"pi_overview_{uuid.uuid4().hex[:12]}",
        stripe_charge_id=f"ch_overview_{uuid.uuid4().hex[:12]}",
        amount_usd=amount_usd,
        commission_rate=commission_rate,
        commission_usd=commission_usd,
        tax_rate=tax_rate,
        tax_usd=tax_usd,
        advisor_payout_usd=advisor_payout_usd,
        status=TransactionStatus.succeeded,
        invoice_number=invoice_number,
    )
    txn.created_at = txn_at
    session.add(txn)
    await session.flush()


async def seed_overview_analytics() -> list[str]:
    lines: list[str] = []
    password_hash = hash_password(PASSWORD)
    specs = _build_specs()
    pay_amounts = list(TODAY_AMOUNTS)
    pay_i = 0

    async with async_session_factory() as session:
        cleared = await _clear_prior(session)
        lines.append(f"cleared_prior_users={cleared}")

        advisor = await _ensure_advisor(session, password_hash)

        seekers_n = 0
        assessments_n = 0
        bookings_n = 0
        activity_n = 0
        revenue_today = 0.0

        for spec in specs:
            user, registered_at = await _add_seeker(session, spec, password_hash)
            seekers_n += 1
            signup_day = registered_at.date()
            await _add_activity(session, user, signup_day, spec.return_offsets)
            activity_n += 1 + sum(
                1
                for n in spec.return_offsets
                if signup_day + timedelta(days=n) <= datetime.now(UTC).date()
            )

            if spec.with_assessment:
                await _add_assessment(session, user, registered_at)
                assessments_n += 1

            if spec.with_booking:
                if spec.revenue_today:
                    amount = pay_amounts[pay_i]
                    pay_i += 1
                    scheduled = datetime.now(UTC) - timedelta(hours=2 + pay_i)
                    txn_at = datetime.now(UTC) - timedelta(minutes=10 * pay_i)
                    await _add_booking_and_txn(
                        session,
                        seeker=user,
                        advisor=advisor,
                        scheduled=scheduled,
                        amount_usd=amount,
                        txn_at=txn_at,
                    )
                    revenue_today += amount
                else:
                    scheduled = registered_at + timedelta(days=1, hours=2)
                    await _add_booking_and_txn(
                        session,
                        seeker=user,
                        advisor=advisor,
                        scheduled=scheduled,
                        amount_usd=99.0,
                        txn_at=scheduled,
                    )
                bookings_n += 1

        await session.commit()
        lines.append(f"advisor={ADVISOR_EMAIL}")
        lines.append(f"seekers={seekers_n}")
        lines.append(f"assessments={assessments_n}")
        lines.append(f"bookings={bookings_n}")
        lines.append(f"activity_cohorts={activity_n}")
        lines.append(f"revenue_today_usd={round(revenue_today, 2)}")
        lines.append(f"countries={len(COUNTRY_COUNTS)}")
        lines.append(f"password={PASSWORD}")
    return lines


async def main() -> None:
    try:
        for line in await seed_overview_analytics():
            print(line)
        print()
        print("Overview Analytics: GET /api/v1/admin/analytics/overview")
        print("  default window: ?days=30")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
