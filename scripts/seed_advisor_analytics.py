"""Seed data for the admin Advisor Analytics tab.

Populates ``GET /api/v1/admin/analytics/advisors`` with:
  - top_rated_advisors (email, avatar_url, avg_rating, review_count)
  - session_completed_pct (completed vs non-pending in the window)
  - session_trend (completed sessions per month → ``{month, value}``)

Run with::

    uv run python -m scripts.seed_advisor_analytics

Idempotent: deletes prior ``advisor.analytics.seed.*`` users (and cascaded
rows), then recreates. Password: TestPass123!

Use ``?days=120`` on the advisors endpoint so the 4-month session trend
appears (endpoint default is 30 days).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.review import ModerationStatus, Review
from app.models.user import User, UserRole, VerificationStatus
from app.services import booking_service
from app.services.advisor_search_service import generate_unique_slug

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
EMAIL_PREFIX = "advisor.analytics.seed."
SEEKER_EMAIL = f"{EMAIL_PREFIX}seeker@globlejump.test"

# Placeholder avatars (deterministic per advisor) so FE UserCell is not empty.
_AVATAR = "https://i.pravatar.cc/150?u={slug}"


@dataclass(frozen=True)
class AdvisorSpec:
    local: str
    full_name: str
    title: str
    # Each int is a 1–5 overall used for every dimension on that review.
    review_ratings: tuple[int, ...]
    # Completed sessions per month-ago index (0=current … 3=three months ago).
    completed_by_month: tuple[int, int, int, int]
    # Cancelled (non-pending) sessions in the last ~25 days — lowers completion %.
    cancelled_recent: int = 0


ADVISORS: list[AdvisorSpec] = [
    AdvisorSpec(
        local="nova",
        full_name="Nova Chen",
        title="US Immigration Attorney",
        review_ratings=(5, 5, 5, 4, 5),
        completed_by_month=(14, 12, 11, 10),
        cancelled_recent=2,
    ),
    AdvisorSpec(
        local="omar",
        full_name="Omar Haddad",
        title="Gulf Visa Specialist",
        review_ratings=(5, 4, 5, 4),
        completed_by_month=(11, 10, 9, 8),
        cancelled_recent=3,
    ),
    AdvisorSpec(
        local="priya",
        full_name="Priya Nair",
        title="Canada Express Entry Coach",
        review_ratings=(4, 4, 5, 4, 4),
        completed_by_month=(9, 8, 10, 7),
        cancelled_recent=1,
    ),
    AdvisorSpec(
        local="lucas",
        full_name="Lucas Meyer",
        title="EU Blue Card Advisor",
        review_ratings=(4, 3, 4, 4),
        completed_by_month=(7, 6, 5, 6),
        cancelled_recent=4,
    ),
    AdvisorSpec(
        local="aisha",
        full_name="Aisha Okonkwo",
        title="UK Skilled Worker Guide",
        review_ratings=(3, 4, 3),
        completed_by_month=(5, 4, 4, 3),
        cancelled_recent=2,
    ),
]


def _email(local: str) -> str:
    return f"{EMAIL_PREFIX}{local}@globlejump.test"


def _month_anchor(months_ago: int) -> datetime:
    """Mid-month UTC anchor ``months_ago`` calendar months before now."""
    now = datetime.now(UTC)
    year, month = now.year, now.month - months_ago
    while month <= 0:
        month += 12
        year -= 1
    return datetime(year, month, 15, 12, 0, tzinfo=UTC)


async def _clear_prior(session: AsyncSession) -> int:
    users = (
        (await session.execute(select(User).where(User.email.like(f"{EMAIL_PREFIX}%"))))
        .scalars()
        .all()
    )
    if not users:
        return 0
    ids = [u.id for u in users]
    # Reviews / bookings reference users; delete bookings first (reviews cascade
    # via booking or advisor). Explicit review delete keeps SQLite/PG tidy.
    await session.execute(
        delete(Review).where(Review.advisor_id.in_(ids) | Review.seeker_id.in_(ids))
    )
    await session.execute(
        delete(Booking).where(Booking.advisor_id.in_(ids) | Booking.seeker_id.in_(ids))
    )
    await session.execute(delete(User).where(User.id.in_(ids)))
    await session.flush()
    return len(ids)


async def _ensure_seeker(session: AsyncSession, password_hash: str) -> User:
    user = User(
        email=SEEKER_EMAIL,
        full_name="Advisor Analytics Seed Seeker",
        hashed_password=password_hash,
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC),
        verification_status=VerificationStatus.approved,
    )
    session.add(user)
    await session.flush()
    return user


async def _create_advisor(
    session: AsyncSession, spec: AdvisorSpec, password_hash: str
) -> User:
    user = User(
        email=_email(spec.local),
        full_name=spec.full_name,
        hashed_password=password_hash,
        role=UserRole.advisor,
        is_active=True,
        email_verified_at=datetime.now(UTC) - timedelta(days=60),
        verification_status=VerificationStatus.approved,
    )
    session.add(user)
    await session.flush()

    slug = await generate_unique_slug(session, spec.full_name)
    session.add(
        AdvisorProfile(
            user_id=user.id,
            title=spec.title,
            bio=f"Seed profile for admin advisor analytics — {spec.title}.",
            profile_photo_url=_AVATAR.format(slug=spec.local),
            country_of_residence="CA",
            years_of_experience=8,
            successful_applications=120,
            successful_application_rate=88.0,
            public_profile_slug=slug,
        )
    )
    await session.flush()
    logger.info("advisor_analytics_advisor_created", email=user.email)
    return user


async def _add_booking(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    scheduled: datetime,
    status: BookingStatus,
) -> Booking:
    end = scheduled + timedelta(minutes=45)
    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type="immigration_specialist",
        duration_minutes=45,
        price_usd=99.0,
        scheduled_start=scheduled,
        scheduled_end=end,
        status=status,
        payment_status=(
            PaymentStatus.paid if status == BookingStatus.completed else PaymentStatus.unpaid
        ),
    )
    session.add(booking)
    await session.flush()
    return booking


async def _seed_sessions_and_reviews(
    session: AsyncSession, *, advisor: User, seeker: User, spec: AdvisorSpec
) -> tuple[int, int, int]:
    completed_n = 0
    cancelled_n = 0
    review_n = 0

    for months_ago, volume in enumerate(spec.completed_by_month):
        anchor = _month_anchor(months_ago)
        for i in range(volume):
            scheduled = anchor + timedelta(hours=i)
            booking = await _add_booking(
                session,
                seeker=seeker,
                advisor=advisor,
                scheduled=scheduled,
                status=BookingStatus.completed,
            )
            completed_n += 1
            # Attach reviews to the most recent month's first N bookings.
            if months_ago == 0 and i < len(spec.review_ratings):
                rating = spec.review_ratings[i]
                session.add(
                    Review(
                        booking_id=booking.id,
                        seeker_id=seeker.id,
                        advisor_id=advisor.id,
                        rating_expertise=rating,
                        rating_communication=rating,
                        rating_professionalism=rating,
                        rating_value=rating,
                        rating_overall=float(rating),
                        text=f"Seed review ({rating}/5) for {spec.full_name}",
                        is_verified=True,
                        moderation_status=ModerationStatus.visible,
                        created_by=seeker.id,
                    )
                )
                review_n += 1

    # Recent cancellations (within default 30-day window) for completion %.
    now = datetime.now(UTC)
    for i in range(spec.cancelled_recent):
        await _add_booking(
            session,
            seeker=seeker,
            advisor=advisor,
            scheduled=now - timedelta(days=3 + i, hours=i),
            status=BookingStatus.cancelled,
        )
        cancelled_n += 1

    await session.flush()
    return completed_n, cancelled_n, review_n


async def seed_advisor_analytics() -> list[str]:
    lines: list[str] = []
    password_hash = hash_password(PASSWORD)

    async with async_session_factory() as session:
        cleared = await _clear_prior(session)
        lines.append(f"cleared_prior_users={cleared}")

        seeker = await _ensure_seeker(session, password_hash)
        total_completed = 0
        total_cancelled = 0
        total_reviews = 0

        for spec in ADVISORS:
            advisor = await _create_advisor(session, spec, password_hash)
            completed, cancelled, reviews = await _seed_sessions_and_reviews(
                session, advisor=advisor, seeker=seeker, spec=spec
            )
            total_completed += completed
            total_cancelled += cancelled
            total_reviews += reviews
            avg = round(sum(spec.review_ratings) / len(spec.review_ratings), 2)
            lines.append(
                f"advisor={advisor.email} avg≈{avg} reviews={reviews} "
                f"completed={completed} cancelled={cancelled}"
            )

        await session.commit()
        lines.append(f"seeker={SEEKER_EMAIL}")
        lines.append(f"advisors={len(ADVISORS)}")
        lines.append(f"completed_sessions={total_completed}")
        lines.append(f"cancelled_sessions={total_cancelled}")
        lines.append(f"reviews={total_reviews}")
        lines.append(f"password={PASSWORD}")
    return lines


async def main() -> None:
    try:
        for line in await seed_advisor_analytics():
            print(line)
        print()
        print("Advisor Analytics: GET /api/v1/admin/analytics/advisors?days=120")
        print("  (default days=30 only covers ~1 month of session_trend)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
