"""Seed bookable services + weekly availability for FE Book-a-Consultation testing.

Targets the demo advisor ``search.advisor@globlejump.test`` (and ensures a
demo seeker can bookmark them). Idempotent.

    uv run python -m scripts.seed_booking_demo
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_availability import AdvisorWeeklySlot
from app.models.advisor_bookmark import AdvisorBookmark
from app.models.advisor_profile import (
    AdvisorOfferedService,
    AdvisorProfile,
    AdvisorService,
    AdvisorServiceType,
)
from app.models.user import User, UserRole, VerificationStatus

logger = get_logger(__name__)

ADVISOR_EMAIL = "search.advisor@globlejump.test"
SEEKER_EMAIL = "search.seeker@globlejump.test"
PASSWORD = "TestPass123!"

# Mon–Fri 09:00–17:00 America/Toronto — free_slots expands these into UTC.
WEEKLY_SLOTS = [(weekday, time(9, 0), time(17, 0), "America/Toronto") for weekday in range(5)]

# Bookable offerings (POST /bookings resolves these by service_type).
# Must match AdvisorServiceType (advisor onboarding enum) — no free-form values.
BOOKABLE_SERVICES: list[tuple[str, int, float]] = [
    (AdvisorServiceType.immigration_specialist.value, 30, 49.0),
    (AdvisorServiceType.career_coach.value, 60, 149.0),
    (AdvisorServiceType.resume_writer.value, 45, 89.0),
]

# Onboarding category tags (also surfaced in offered_services[]).
OFFERED_CATEGORIES = [
    AdvisorServiceType.immigration_specialist,
    AdvisorServiceType.career_coach,
    AdvisorServiceType.resume_writer,
]


async def _ensure_advisor(session: AsyncSession) -> User:
    user = await session.scalar(select(User).where(User.email == ADVISOR_EMAIL))
    if user is None:
        user = User(
            email=ADVISOR_EMAIL,
            full_name="Search Demo Advisor",
            hashed_password=hash_password(PASSWORD),
            role=UserRole.advisor,
            is_active=True,
            email_verified_at=datetime.now(UTC),
            verification_status=VerificationStatus.approved,
        )
        session.add(user)
        await session.flush()
        session.add(AdvisorProfile(user_id=user.id, title="Immigration Consultant"))
        await session.flush()
        logger.info("booking_demo_advisor_created", email=ADVISOR_EMAIL, id=str(user.id))
    else:
        user.is_active = True
        user.verification_status = VerificationStatus.approved
        session.add(user)
    return user


async def _ensure_seeker(session: AsyncSession) -> User:
    user = await session.scalar(select(User).where(User.email == SEEKER_EMAIL))
    if user is None:
        user = User(
            email=SEEKER_EMAIL,
            full_name="Search Demo Seeker",
            hashed_password=hash_password(PASSWORD),
            role=UserRole.seeker,
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        logger.info("booking_demo_seeker_created", email=SEEKER_EMAIL, id=str(user.id))
    return user


async def _seed_services_and_slots(session: AsyncSession, advisor: User) -> None:
    profile = await session.scalar(
        select(AdvisorProfile).where(AdvisorProfile.user_id == advisor.id)
    )
    assert profile is not None

    # Replace bookable services
    existing_services = (
        await session.execute(select(AdvisorService).where(AdvisorService.profile_id == profile.id))
    ).scalars().all()
    for row in existing_services:
        await session.delete(row)
    await session.flush()
    for st, dur, price in BOOKABLE_SERVICES:
        session.add(
            AdvisorService(
                profile_id=profile.id,
                service_type=st,
                duration_minutes=dur,
                price_usd=price,
            )
        )

    # Replace offered category tags
    existing_offered = (
        await session.execute(
            select(AdvisorOfferedService).where(AdvisorOfferedService.profile_id == profile.id)
        )
    ).scalars().all()
    for row in existing_offered:
        await session.delete(row)
    await session.flush()
    for cat in OFFERED_CATEGORIES:
        session.add(AdvisorOfferedService(profile_id=profile.id, service_type=cat.value))

    # Replace weekly availability
    await session.execute(
        delete(AdvisorWeeklySlot).where(AdvisorWeeklySlot.advisor_id == advisor.id)
    )
    await session.flush()
    for weekday, start, end, tz in WEEKLY_SLOTS:
        session.add(
            AdvisorWeeklySlot(
                advisor_id=advisor.id,
                weekday=weekday,
                start_time=start,
                end_time=end,
                timezone=tz,
            )
        )
    await session.flush()


async def _ensure_bookmark(session: AsyncSession, seeker: User, advisor: User) -> None:
    existing = await session.scalar(
        select(AdvisorBookmark).where(
            AdvisorBookmark.seeker_id == seeker.id,
            AdvisorBookmark.advisor_id == advisor.id,
            AdvisorBookmark.is_archived.is_(False),
        )
    )
    if existing is not None:
        return
    session.add(
        AdvisorBookmark(
            seeker_id=seeker.id,
            advisor_id=advisor.id,
            created_by=seeker.id,
        )
    )
    await session.flush()
    logger.info("booking_demo_bookmark_created", seeker=SEEKER_EMAIL, advisor=ADVISOR_EMAIL)


async def seed_booking_demo() -> list[str]:
    lines: list[str] = []
    async with async_session_factory() as session:
        advisor = await _ensure_advisor(session)
        seeker = await _ensure_seeker(session)
        await _seed_services_and_slots(session, advisor)
        await _ensure_bookmark(session, seeker, advisor)
        await session.commit()
        lines.append(f"advisor_id={advisor.id}  email={ADVISOR_EMAIL}  pass={PASSWORD}")
        lines.append(f"seeker_id={seeker.id}   email={SEEKER_EMAIL}   pass={PASSWORD}")
        lines.append(
            "services="
            + ", ".join(f"{st}({dur}m/${price})" for st, dur, price in BOOKABLE_SERVICES)
        )
        lines.append("availability=Mon–Fri 09:00–17:00 America/Toronto")
        lines.append("bookmark=seeker→advisor ensured")
    await engine.dispose()
    return lines


async def main() -> None:
    for line in await seed_booking_demo():
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
