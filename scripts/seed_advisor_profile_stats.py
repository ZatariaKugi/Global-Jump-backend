"""Seed profile stats + reviews for a specific advisor (FE stats cards).

Sets ``successful_applications`` and creates approved reviews so
``GET /advisors/me/profile`` returns real ``average_rating`` / ``review_count``.

    uv run python -m scripts.seed_advisor_profile_stats

Override target with env ``ADVISOR_USER_ID``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.review import ModerationStatus, Review
from app.models.user import User, UserRole
from app.services import booking_service, review_service

logger = get_logger(__name__)

DEFAULT_ADVISOR_ID = uuid.UUID("53b7d621-077e-475c-8fd1-bb01e2bd6370")
SEEKER_EMAIL = "stats.seeker.seed@globlejump.test"
PASSWORD = "TestPass123!"

# 12 reviews targeting ~4.8 overall average.
REVIEW_DIMS: list[tuple[int, int, int, int, str]] = [
    (5, 5, 5, 5, "Outstanding consultation — highly recommend."),
    (5, 5, 5, 4, "Clear guidance and very professional."),
    (5, 4, 5, 5, "Helped me prepare every document."),
    (5, 5, 4, 5, "Responsive and knowledgeable."),
    (4, 5, 5, 5, "Great experience overall."),
    (5, 5, 5, 5, "Best advisor I've worked with."),
    (5, 4, 5, 4, "Solid advice on my student visa."),
    (5, 5, 5, 4, "Thorough and patient."),
    (4, 5, 5, 5, "Would book again."),
    (5, 5, 4, 5, "Excellent communication."),
    (5, 5, 5, 5, "Made the process stress-free."),
    (5, 4, 5, 5, "Very strong expertise."),
]


async def _ensure_seeker(session) -> User:
    user = await session.scalar(select(User).where(User.email == SEEKER_EMAIL))
    if user is not None:
        return user
    user = User(
        email=SEEKER_EMAIL,
        full_name="Stats Seed Seeker",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    logger.info("stats_seeker_created", email=SEEKER_EMAIL)
    return user


async def _create_booking(
    session, *, advisor: User, seeker: User, days_ago: int
) -> Booking:
    start = datetime.now(UTC) - timedelta(days=days_ago)
    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type="immigration_specialist",
        duration_minutes=30,
        price_usd=75.0,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=30),
        status=BookingStatus.completed,
        payment_status=PaymentStatus.paid,
        created_by=seeker.id,
    )
    session.add(booking)
    await session.flush()
    return booking


async def seed_advisor_profile_stats(advisor_id: uuid.UUID) -> list[str]:
    lines: list[str] = []
    async with async_session_factory() as session:
        advisor = await session.get(User, advisor_id)
        if advisor is None or advisor.role != UserRole.advisor:
            raise SystemExit(f"Advisor not found: {advisor_id}")

        profile = await session.scalar(
            select(AdvisorProfile).where(AdvisorProfile.user_id == advisor_id)
        )
        if profile is None:
            profile = AdvisorProfile(user_id=advisor_id)
            session.add(profile)
            await session.flush()

        profile.successful_applications = 150
        if profile.years_of_experience is None:
            profile.years_of_experience = 21
        if profile.successful_application_rate is None:
            profile.successful_application_rate = 94.0
        session.add(profile)

        # Remove prior seed reviews/bookings for this advisor so re-run is idempotent.
        existing_reviews = (
            await session.execute(select(Review).where(Review.advisor_id == advisor_id))
        ).scalars().all()
        for rev in existing_reviews:
            await session.delete(rev)
        await session.flush()

        seeker = await _ensure_seeker(session)
        overalls: list[float] = []
        for i, (expertise, communication, professionalism, value, text) in enumerate(
            REVIEW_DIMS
        ):
            booking = await _create_booking(
                session, advisor=advisor, seeker=seeker, days_ago=2 + i * 3
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

        await session.commit()

        avg, count = await review_service.rating_summary(session, advisor_id)
        lines.append(f"advisor={advisor.email} id={advisor.id}")
        lines.append(f"years_of_experience={profile.years_of_experience}")
        lines.append(f"successful_applications={profile.successful_applications}")
        lines.append(f"average_rating={avg} review_count={count}")
        mean_raw = round(sum(overalls) / len(overalls), 2)
        lines.append(f"seeded_reviews={len(overalls)} mean_raw={mean_raw}")
    await engine.dispose()
    return lines


async def main() -> None:
    raw = os.environ.get("ADVISOR_USER_ID", str(DEFAULT_ADVISOR_ID))
    advisor_id = uuid.UUID(raw)
    for line in await seed_advisor_profile_stats(advisor_id):
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
