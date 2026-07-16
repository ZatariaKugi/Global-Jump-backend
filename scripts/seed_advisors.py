"""Seed 5 fully-onboarded, verified advisor accounts for local testing.

Each advisor gets: a verified/active User account, a complete AdvisorProfile
(title, bio, country_of_residence, specializations, country expertise,
languages, service offerings), one verified credential document, a weekly
availability schedule, sample succeeded earnings (booking + transaction),
and sample reviews (so average rating shows on search / profile cards).

Run with:
    uv run python -m scripts.seed_advisors

Idempotent: re-running creates missing advisors, backfills country / earnings
/ reviews on existing seed advisors, and skips rows that already have those
values. All accounts share the password printed at the end (or set via
SEED_ADVISOR_PASSWORD).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_availability import AdvisorWeeklySlot
from app.models.advisor_credential import AdvisorCredential, CredentialStatus, DocumentType
from app.models.advisor_profile import (
    AdvisorCountryExpertise,
    AdvisorLanguage,
    AdvisorProfile,
    AdvisorService,
    AdvisorVisaSpecialization,
)
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.review import ModerationStatus, Review
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole, VerificationStatus
from app.services import booking_service
from app.services.advisor_search_service import generate_unique_slug

logger = get_logger(__name__)

DEFAULT_PASSWORD = "TestPass123!"
SEED_SEEKER_EMAIL = "seeker.earnings.seed@globlejump.test"

# Each review tuple: (expertise, communication, professionalism, value, text)
# Overall = mean of the four dims; used by search/profile average_rating.
ADVISORS = [
    {
        "email": "advisor1.seed@globlejump.test",
        "full_name": "Sarah Mitchell",
        "title": "Canadian Immigration Consultant",
        "bio": "10+ years helping clients navigate Express Entry, PNP, and study permits.",
        "country_of_residence": "CA",
        "years_of_experience": 11,
        "successful_applications": 340,
        "specializations": ["work", "student", "permanent_residency"],
        "countries": ["CA", "US"],
        "languages": [("English", "native"), ("French", "fluent")],
        "services": [
            ("consultation", 30, 49.0),
            ("full_application_review", 90, 249.0),
        ],
        "seed_earnings_usd": 1250.00,
        "successful_application_rate": 94.0,
        "seed_reviews": [
            (5, 5, 5, 5, "Sarah made Express Entry feel manageable."),
            (5, 4, 5, 5, "Clear advice and fast follow-ups."),
            (4, 5, 5, 4, "Very knowledgeable about PNPs."),
        ],
    },
    {
        "email": "advisor2.seed@globlejump.test",
        "full_name": "James Okafor",
        "title": "US Immigration Attorney",
        "bio": "Specializes in H-1B, O-1, and family-based petitions.",
        "country_of_residence": "US",
        "years_of_experience": 8,
        "successful_applications": 210,
        "specializations": ["work", "family"],
        "countries": ["US"],
        "languages": [("English", "native")],
        "services": [
            ("consultation", 30, 75.0),
            ("case_strategy_session", 60, 150.0),
        ],
        "seed_earnings_usd": 980.50,
        "successful_application_rate": 88.5,
        "seed_reviews": [
            (5, 5, 4, 4, "Great H-1B strategy session."),
            (4, 4, 5, 4, "Professional and thorough."),
            (5, 4, 4, 4, None),
        ],
    },
    {
        "email": "advisor3.seed@globlejump.test",
        "full_name": "Priya Nair",
        "title": "UK Visa & Settlement Advisor",
        "bio": "Skilled Worker, Student, and Family visa specialist registered with OISC.",
        "country_of_residence": "GB",
        "years_of_experience": 6,
        "successful_applications": 150,
        "specializations": ["work", "student", "family"],
        "countries": ["GB"],
        "languages": [("English", "native"), ("Hindi", "fluent")],
        "services": [
            ("consultation", 30, 40.0),
            ("document_review", 45, 90.0),
        ],
        "seed_earnings_usd": 640.00,
        "successful_application_rate": 91.0,
        "seed_reviews": [
            (5, 5, 5, 5, "Excellent UK Skilled Worker guidance."),
            (5, 5, 4, 5, "Document checklist was spot on."),
            (4, 5, 5, 5, "Would book again."),
            (5, 4, 5, 4, None),
        ],
    },
    {
        "email": "advisor4.seed@globlejump.test",
        "full_name": "Liam Bennett",
        "title": "Australian Migration Agent",
        "bio": "MARA-registered agent focused on skilled migration and partner visas.",
        "country_of_residence": "AU",
        "years_of_experience": 13,
        "successful_applications": 400,
        "specializations": ["work", "permanent_residency", "family"],
        "countries": ["AU"],
        "languages": [("English", "native")],
        "services": [
            ("consultation", 30, 60.0),
            ("full_application_review", 90, 280.0),
        ],
        "seed_earnings_usd": 2100.75,
        "successful_application_rate": 96.0,
        "seed_reviews": [
            (5, 5, 5, 4, "Liam knows AU migration inside out."),
            (4, 5, 5, 5, "Partner visa process explained clearly."),
            (5, 5, 5, 5, "Worth every dollar."),
        ],
    },
    {
        "email": "advisor5.seed@globlejump.test",
        "full_name": "Elena Rossi",
        "title": "Multi-Country Investment & Study Visa Advisor",
        "bio": "Helps clients with investment visas and study pathways across CA, UK, and AU.",
        "country_of_residence": "IT",
        "years_of_experience": 9,
        "successful_applications": 175,
        "specializations": ["investment", "student"],
        "countries": ["CA", "GB", "AU"],
        "languages": [("English", "native"), ("Italian", "fluent")],
        "services": [
            ("consultation", 30, 55.0),
            ("case_strategy_session", 60, 160.0),
        ],
        "seed_earnings_usd": 875.25,
        "successful_application_rate": 87.0,
        "seed_reviews": [
            (4, 4, 5, 4, "Helpful multi-country comparison."),
            (5, 4, 4, 4, "Solid study pathway advice."),
            (4, 5, 4, 4, None),
        ],
    },
]

# Mon–Fri, 09:00–17:00 in the advisor's local timezone
WEEKLY_SLOTS = [(weekday, time(9, 0), time(17, 0), "America/Toronto") for weekday in range(5)]


async def _get_or_create_seed_seeker(session: AsyncSession, password_hash: str) -> User:
    seeker = await session.scalar(select(User).where(User.email == SEED_SEEKER_EMAIL))
    if seeker is not None:
        return seeker
    seeker = User(
        email=SEED_SEEKER_EMAIL,
        full_name="Seed Earnings Seeker",
        hashed_password=password_hash,
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(seeker)
    await session.flush()
    return seeker


async def _ensure_country(session: AsyncSession, user: User, country: str) -> None:
    profile = await session.scalar(
        select(AdvisorProfile).where(AdvisorProfile.user_id == user.id)
    )
    if profile is None:
        return
    if profile.country_of_residence == country:
        return
    profile.country_of_residence = country
    session.add(profile)
    await session.flush()
    logger.info("advisor_country_backfilled", email=user.email, country=country)


async def _ensure_success_rate(session: AsyncSession, user: User, rate: float) -> None:
    profile = await session.scalar(
        select(AdvisorProfile).where(AdvisorProfile.user_id == user.id)
    )
    if profile is None:
        return
    if profile.successful_application_rate is not None:
        return
    profile.successful_application_rate = rate
    session.add(profile)
    await session.flush()
    logger.info("advisor_success_rate_seeded", email=user.email, rate=rate)


async def _ensure_earnings(
    session: AsyncSession,
    *,
    advisor: User,
    seeker: User,
    payout_usd: float,
) -> None:
    """Create one completed paid booking + succeeded transaction if advisor has none."""
    existing = await session.scalar(
        select(func.count())
        .select_from(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(
            Booking.advisor_id == advisor.id,
            Transaction.status == TransactionStatus.succeeded,
        )
    )
    if existing and existing > 0:
        logger.info("advisor_earnings_skipped_exists", email=advisor.email)
        return

    commission_rate = 0.15
    tax_rate = 0.08
    # Reverse from advisory payout so ``earnings`` matches seed_earnings_usd.
    # payout = amount - amount*commission - amount*tax = amount * (1 - c - t)
    amount_usd = round(payout_usd / (1.0 - commission_rate - tax_rate), 2)
    commission_usd = round(amount_usd * commission_rate, 2)
    tax_usd = round(amount_usd * tax_rate, 2)
    # Fix rounding so advisor_payout matches intended seed value.
    advisor_payout_usd = round(amount_usd - commission_usd - tax_usd, 2)
    if advisor_payout_usd != payout_usd:
        advisor_payout_usd = payout_usd

    start = datetime.now(UTC) - timedelta(days=7)
    end = start + timedelta(minutes=30)
    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type="consultation",
        duration_minutes=30,
        price_usd=amount_usd,
        scheduled_start=start,
        scheduled_end=end,
        status=BookingStatus.completed,
        payment_status=PaymentStatus.paid,
    )
    session.add(booking)
    await session.flush()

    max_invoice = await session.scalar(select(func.max(Transaction.invoice_number)))
    invoice_number = (max_invoice or 1000) + 1

    session.add(
        Transaction(
            booking_id=booking.id,
            stripe_checkout_session_id=f"cs_seed_{uuid.uuid4().hex[:12]}",
            stripe_payment_intent_id=f"pi_seed_{uuid.uuid4().hex[:12]}",
            stripe_charge_id=f"ch_seed_{uuid.uuid4().hex[:12]}",
            amount_usd=amount_usd,
            commission_rate=commission_rate,
            commission_usd=commission_usd,
            tax_rate=tax_rate,
            tax_usd=tax_usd,
            advisor_payout_usd=advisor_payout_usd,
            status=TransactionStatus.succeeded,
            invoice_number=invoice_number,
            created_at=start,
        )
    )
    await session.flush()
    logger.info(
        "advisor_earnings_seeded",
        email=advisor.email,
        earnings=advisor_payout_usd,
    )


async def _create_review_booking(
    session: AsyncSession,
    *,
    advisor: User,
    seeker: User,
    days_ago: int,
) -> Booking:
    """Completed paid booking used as the host for a seed review."""
    start = datetime.now(UTC) - timedelta(days=days_ago)
    end = start + timedelta(minutes=30)
    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type="consultation",
        duration_minutes=30,
        price_usd=50.0,
        scheduled_start=start,
        scheduled_end=end,
        status=BookingStatus.completed,
        payment_status=PaymentStatus.paid,
    )
    session.add(booking)
    await session.flush()
    return booking


async def _ensure_reviews(
    session: AsyncSession,
    *,
    advisor: User,
    seeker: User,
    reviews: list[tuple[int, int, int, int, str | None]],
) -> None:
    """Attach sample reviews so average_rating / review_count are populated.

    Reuses existing completed bookings that have no review first, then creates
    additional bookings as needed. Idempotent when the advisor already has any
    reviews.
    """
    existing_count = await session.scalar(
        select(func.count()).select_from(Review).where(Review.advisor_id == advisor.id)
    )
    if existing_count and existing_count > 0:
        logger.info("advisor_reviews_skipped_exists", email=advisor.email, count=existing_count)
        return

    # Prefer completed bookings that still lack a review (e.g. earnings seed booking).
    unused_bookings = list(
        (
            await session.execute(
                select(Booking)
                .where(
                    Booking.advisor_id == advisor.id,
                    Booking.status == BookingStatus.completed,
                    ~Booking.id.in_(select(Review.booking_id)),
                )
                .order_by(Booking.scheduled_start.asc())
            )
        ).scalars()
    )

    overalls: list[float] = []
    for i, (expertise, communication, professionalism, value, text) in enumerate(reviews):
        if unused_bookings:
            booking = unused_bookings.pop(0)
        else:
            booking = await _create_review_booking(
                session,
                advisor=advisor,
                seeker=seeker,
                days_ago=3 + i * 4,
            )

        overall = round(
            (expertise + communication + professionalism + value) / 4,
            2,
        )
        overalls.append(overall)
        session.add(
            Review(
                booking_id=booking.id,
                seeker_id=seeker.id,
                advisor_id=advisor.id,
                rating_expertise=expertise,
                rating_communication=communication,
                rating_professionalism=professionalism,
                rating_value=value,
                rating_overall=overall,
                text=text,
                is_verified=True,
                moderation_status=ModerationStatus.visible,
                created_by=seeker.id,
            )
        )

    await session.flush()
    avg = round(sum(overalls) / len(overalls), 2) if overalls else None
    logger.info(
        "advisor_reviews_seeded",
        email=advisor.email,
        count=len(overalls),
        avg_rating=avg,
    )


async def _create_advisor(
    session: AsyncSession, data: dict[str, Any], password_hash: str
) -> User:
    user = User(
        email=data["email"],
        full_name=data["full_name"],
        hashed_password=password_hash,
        role=UserRole.advisor,
        is_active=True,
        email_verified_at=datetime.now(UTC),
        verification_status=VerificationStatus.approved,
    )
    session.add(user)
    await session.flush()

    slug = await generate_unique_slug(session, data["full_name"])
    profile = AdvisorProfile(
        user_id=user.id,
        title=data["title"],
        bio=data["bio"],
        country_of_residence=data["country_of_residence"],
        years_of_experience=data["years_of_experience"],
        successful_applications=data["successful_applications"],
        successful_application_rate=float(data["successful_application_rate"]),
        public_profile_slug=slug,
    )
    session.add(profile)
    await session.flush()

    for s in data["specializations"]:
        session.add(AdvisorVisaSpecialization(profile_id=profile.id, specialization=s))
    for c in data["countries"]:
        session.add(AdvisorCountryExpertise(profile_id=profile.id, country_code=c))
    for lang, prof in data["languages"]:
        session.add(AdvisorLanguage(profile_id=profile.id, language=lang, proficiency=prof))
    for st, dur, price in data["services"]:
        session.add(
            AdvisorService(
                profile_id=profile.id, service_type=st, duration_minutes=dur, price_usd=price
            )
        )

    session.add(
        AdvisorCredential(
            user_id=user.id,
            document_type=DocumentType.immigration_license,
            document_name=f"{data['full_name']} - License.pdf",
            file_url=f"/uploads/credentials/{user.id}/seed-license.pdf",
            status=CredentialStatus.verified,
            verified_at=datetime.now(UTC),
        )
    )

    for weekday, start, end, tz in WEEKLY_SLOTS:
        session.add(
            AdvisorWeeklySlot(
                advisor_id=user.id,
                weekday=weekday,
                start_time=start,
                end_time=end,
                timezone=tz,
            )
        )

    await session.flush()
    logger.info("advisor_seeded", email=data["email"], slug=slug)
    return user


async def _seed_one(
    session: AsyncSession,
    data: dict[str, Any],
    password_hash: str,
    seeker: User,
) -> None:
    user = await session.scalar(select(User).where(User.email == data["email"]))
    if user is None:
        user = await _create_advisor(session, data, password_hash)
    else:
        logger.info("advisor_seed_exists_enriching", email=data["email"])

    await _ensure_country(session, user, data["country_of_residence"])
    await _ensure_success_rate(session, user, float(data["successful_application_rate"]))
    await _ensure_earnings(
        session,
        advisor=user,
        seeker=seeker,
        payout_usd=float(data["seed_earnings_usd"]),
    )
    await _ensure_reviews(
        session,
        advisor=user,
        seeker=seeker,
        reviews=list(data["seed_reviews"]),
    )


async def seed_advisors(password: str) -> None:
    password_hash = hash_password(password)
    async with async_session_factory() as session:
        seeker = await _get_or_create_seed_seeker(session, password_hash)
        for data in ADVISORS:
            await _seed_one(session, data, password_hash, seeker)
        await session.commit()


async def main() -> None:
    password = os.environ.get("SEED_ADVISOR_PASSWORD", DEFAULT_PASSWORD)
    try:
        await seed_advisors(password)
    finally:
        await engine.dispose()
    print(f"Done. All seeded advisors share the password: {password}")


if __name__ == "__main__":
    asyncio.run(main())
