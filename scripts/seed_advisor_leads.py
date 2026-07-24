"""Seed AI-matched advisor leads (all statuses) for FE testing.

Creates seekers + completed assessments + ``AdvisorLead`` rows for a target
advisor. Where useful, also creates a real booking so list/detail return
``appointment_id`` / ``booking_id``.

    uv run python -m scripts.seed_advisor_leads

Override target with env ``ADVISOR_USER_ID`` (defaults to the requested demo id).
Idempotent: clears prior seed leads/assessments/bookings for the seed seekers
tied to this advisor, then recreates.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_lead import AdvisorLead, AdvisorLeadStatus
from app.models.assessment import Assessment, AssessmentStatus, EligibilityTier
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.user import User, UserRole
from app.services import booking_service

logger = get_logger(__name__)

DEFAULT_ADVISOR_ID = uuid.UUID("031021b9-36d4-42cc-b349-a961b00c3237")
PASSWORD = "TestPass123!"
EMAIL_PREFIX = "lead.seed."

# (email_local, full_name, status, match_score, with_booking, country, visa)
SEED_LEADS: list[tuple[str, str, AdvisorLeadStatus, float, bool, str, str]] = [
    (
        "new1",
        "Aisha Khan",
        AdvisorLeadStatus.new,
        92.5,
        False,
        "AU",
        "work",
    ),
    (
        "new2",
        "Luis Romero",
        AdvisorLeadStatus.new,
        81.0,
        False,
        "AU",
        "tourist",
    ),
    (
        "viewed1",
        "Mei Chen",
        AdvisorLeadStatus.viewed,
        88.0,
        True,
        "AU",
        "work",
    ),
    (
        "contacted1",
        "Omar Haddad",
        AdvisorLeadStatus.contacted,
        95.0,
        True,
        "AU",
        "work",
    ),
    (
        "contacted2",
        "Priya Nair",
        AdvisorLeadStatus.contacted,
        76.5,
        True,
        "AU",
        "tourist",
    ),
    (
        "dismissed1",
        "Jonas Berg",
        AdvisorLeadStatus.dismissed,
        55.0,
        False,
        "AU",
        "work",
    ),
]


def _email(local: str) -> str:
    return f"{EMAIL_PREFIX}{local}@globlejump.test"


async def _ensure_seeker(session: AsyncSession, local: str, full_name: str) -> User:
    email = _email(local)
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        return user
    user = User(
        email=email,
        full_name=full_name,
        hashed_password=hash_password(PASSWORD),
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    logger.info("lead_seed_seeker_created", email=email)
    return user


async def _clear_prior(
    session: AsyncSession, advisor_id: uuid.UUID, seeker_ids: list[uuid.UUID]
) -> None:
    if not seeker_ids:
        return
    await session.execute(
        delete(AdvisorLead).where(
            AdvisorLead.advisor_id == advisor_id,
            AdvisorLead.seeker_id.in_(seeker_ids),
        )
    )
    await session.execute(
        delete(Booking).where(
            Booking.advisor_id == advisor_id,
            Booking.seeker_id.in_(seeker_ids),
        )
    )
    # Assessments owned by seed seekers (leads FK CASCADE would also clear, but
    # we delete leads first so we can remove orphan assessments cleanly).
    await session.execute(delete(Assessment).where(Assessment.user_id.in_(seeker_ids)))
    await session.flush()


async def _create_assessment(
    session: AsyncSession,
    *,
    seeker: User,
    country: str,
    visa: str,
    days_ago: int,
) -> Assessment:
    created = datetime.now(UTC) - timedelta(days=days_ago)
    assessment = Assessment(
        user_id=seeker.id,
        destination_country=country,
        visa_type=visa,
        status=AssessmentStatus.completed,
        score=78.0,
        tier=EligibilityTier.likely_eligible,
        confidence=0.85,
        completed_at=created + timedelta(minutes=15),
        created_by=seeker.id,
    )
    assessment.created_at = created
    session.add(assessment)
    await session.flush()
    return assessment


async def _create_booking(
    session: AsyncSession,
    *,
    advisor: User,
    seeker: User,
    days_ago: int,
    status: BookingStatus = BookingStatus.confirmed,
) -> Booking:
    start = datetime.now(UTC) + timedelta(days=max(1, 7 - days_ago))
    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type="immigration_specialist",
        duration_minutes=30,
        price_usd=75.0,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=30),
        status=status,
        payment_status=PaymentStatus.paid
        if status == BookingStatus.completed
        else PaymentStatus.unpaid,
        created_by=seeker.id,
    )
    session.add(booking)
    await session.flush()
    return booking


async def seed_advisor_leads(advisor_id: uuid.UUID) -> list[str]:
    lines: list[str] = []
    async with async_session_factory() as session:
        advisor = await session.get(User, advisor_id)
        if advisor is None or advisor.role != UserRole.advisor:
            raise SystemExit(f"Advisor not found: {advisor_id}")

        seekers: list[User] = []
        for local, full_name, *_rest in SEED_LEADS:
            seekers.append(await _ensure_seeker(session, local, full_name))
        seeker_ids = [s.id for s in seekers]
        await _clear_prior(session, advisor_id, seeker_ids)

        for i, (_local, _full_name, status, score, with_booking, country, visa) in enumerate(
            SEED_LEADS
        ):
            seeker = seekers[i]
            assessment = await _create_assessment(
                session,
                seeker=seeker,
                country=country,
                visa=visa,
                days_ago=i + 1,
            )
            booking = None
            if with_booking:
                booking = await _create_booking(
                    session,
                    advisor=advisor,
                    seeker=seeker,
                    days_ago=i + 1,
                    status=BookingStatus.confirmed
                    if status == AdvisorLeadStatus.contacted
                    else BookingStatus.pending,
                )

            reasons = (
                f"Specializes in {country} immigration; "
                f"Specializes in {visa}; high match for seed demo"
            )
            lead = AdvisorLead(
                seeker_id=seeker.id,
                advisor_id=advisor_id,
                assessment_id=assessment.id,
                match_score=score,
                match_reasons=reasons,
                status=status,
                created_by=advisor_id,
            )
            session.add(lead)
            await session.flush()

            appt = (
                booking_service.appointment_id_str(booking) if booking is not None else None
            )
            lines.append(
                f"{status.value:10} score={score:5.1f} seeker={seeker.email} "
                f"appointment_id={appt or 'null'}"
            )
            logger.info(
                "advisor_lead_seeded",
                status=status.value,
                seeker=seeker.email,
                appointment_id=appt,
            )

        await session.commit()
        lines.append(f"advisor_id={advisor_id}")
        lines.append(f"advisor_email={advisor.email}")
        lines.append(f"login_password={PASSWORD} (seed seekers)")
    return lines


async def main() -> None:
    raw = os.environ.get("ADVISOR_USER_ID", str(DEFAULT_ADVISOR_ID))
    advisor_id = uuid.UUID(raw)
    try:
        for line in await seed_advisor_leads(advisor_id):
            print(line)
        print()
        print("List: GET /api/v1/advisors/me/leads  (login as that advisor)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
