"""Seed Advisor Dashboard FE demo data for a single target advisor.

Fills the ``GET /advisors/me/dashboard`` aggregate and its "See all" lists so
``/advisor/dashboard`` renders real data out of the box:

    GET /api/v1/advisors/me/dashboard?days=7|30|90
    GET /api/v1/advisors/me/dashboard/export?days=...
    GET /api/v1/advisors/me/regulatory-updates?days=...
    GET /api/v1/advisors/me/regulatory-updates/{id}
    GET /api/v1/conversations?q=...     (client inquiries / support search)

Creates, for the target advisor:

  - next_upcoming        one confirmed booking a few days out
  - new_leads            three AI-matched leads still in ``new`` status
  - total_earned         a couple of succeeded transactions (last 7 days)
  - pending_reviews      unanswered reviews on completed bookings
  - profile_completion   trims the profile to land at ~80% (drops photo + services)
  - client_inquiries     three threads: new / unread / responded
  - regulatory_updates   several recent global rows

Run with::

    uv run python -m scripts.seed_advisor_dashboard
    ADVISOR_USER_ID=<uuid> uv run python -m scripts.seed_advisor_dashboard

Idempotent: re-running clears the seeded seekers/threads/regulatory rows first.
Seeker password: TestPass123!
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
from app.models.advisor_profile import (
    AdvisorCountryExpertise,
    AdvisorLanguage,
    AdvisorOfferedService,
    AdvisorProfile,
    AdvisorService,
    AdvisorVisaSpecialization,
)
from app.models.assessment import Assessment, AssessmentStatus
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.regulatory_update import RegulatoryUpdate
from app.models.review import ModerationStatus, Review
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole, VerificationStatus
from app.services import booking_service

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
EMAIL_PREFIX = "advisor.dashboard.seed."

# Target advisor whose /advisors/me/dashboard the FE is testing (same default as
# seed_advisor_payments so both scripts describe the same demo advisor).
DEFAULT_ADVISOR_USER_ID = uuid.UUID("da37a676-127b-48a3-8fde-707d7e9df438")

COMMISSION_RATE = 0.15
TAX_RATE = 0.08

# Regulatory feed — country_code, region_label, title, description, days-ago.
_REGULATORY: tuple[tuple[str | None, str, str, str, int], ...] = (
    (
        "CA",
        "Canada",
        "Express Entry draw sizes increased for Q3",
        "IRCC has raised the number of invitations issued per Express Entry draw, "
        "lowering CRS cut-off scores for skilled-worker candidates.",
        2,
    ),
    (
        "AU",
        "Australia",
        "New skilled occupation list takes effect",
        "Several tech and healthcare occupations were added to Australia's skilled "
        "occupation list, expanding eligibility for the subclass 189 visa.",
        5,
    ),
    (
        "GB",
        "United Kingdom",
        "Skilled Worker salary threshold updated",
        "The general salary threshold for the UK Skilled Worker route has been "
        "revised; sponsors should confirm role-specific going rates.",
        9,
    ),
    (
        None,
        "Schengen Area",
        "ETIAS travel authorisation launch window announced",
        "The European Travel Information and Authorisation System (ETIAS) go-live "
        "window has been confirmed for visa-exempt travellers.",
        14,
    ),
    (
        "US",
        "United States",
        "H-1B registration fee change for the next cycle",
        "USCIS published an updated registration fee for the upcoming H-1B "
        "electronic registration period.",
        21,
    ),
)


async def _clear_prior(session: AsyncSession, advisor_id: uuid.UUID) -> int:
    """Remove previously seeded seekers + their bookings/threads/leads/reviews."""
    users = (
        (await session.execute(select(User).where(User.email.like(f"{EMAIL_PREFIX}%"))))
        .scalars()
        .all()
    )
    ids = [u.id for u in users]

    # Regulatory rows are advisor-independent; clear the seeded batch every run.
    await session.execute(
        delete(RegulatoryUpdate).where(RegulatoryUpdate.title.in_([r[2] for r in _REGULATORY]))
    )

    if ids:
        booking_ids = (
            (
                await session.execute(
                    select(Booking.id).where(
                        Booking.advisor_id == advisor_id, Booking.seeker_id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        if booking_ids:
            txn_ids = (
                (
                    await session.execute(
                        select(Transaction.id).where(Transaction.booking_id.in_(booking_ids))
                    )
                )
                .scalars()
                .all()
            )
            if txn_ids:
                await session.execute(delete(Transaction).where(Transaction.id.in_(txn_ids)))
            await session.execute(delete(Review).where(Review.booking_id.in_(booking_ids)))
            await session.execute(delete(Booking).where(Booking.id.in_(booking_ids)))
        await session.execute(
            delete(Message).where(
                Message.conversation_id.in_(
                    select(Conversation.id).where(Conversation.seeker_id.in_(ids))
                )
            )
        )
        await session.execute(delete(Conversation).where(Conversation.seeker_id.in_(ids)))
        await session.execute(delete(AdvisorLead).where(AdvisorLead.seeker_id.in_(ids)))
        await session.execute(delete(Assessment).where(Assessment.user_id.in_(ids)))
        await session.execute(delete(SeekerProfile).where(SeekerProfile.user_id.in_(ids)))
        await session.execute(delete(User).where(User.id.in_(ids)))
    await session.flush()
    return len(ids)


async def _make_seeker(
    session: AsyncSession, *, slug: str, full_name: str, password_hash: str
) -> User:
    user = User(
        email=f"{EMAIL_PREFIX}{slug}@globlejump.test",
        full_name=full_name,
        hashed_password=password_hash,
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC) - timedelta(days=30),
        verification_status=VerificationStatus.approved,
    )
    session.add(user)
    await session.flush()
    session.add(
        SeekerProfile(
            user_id=user.id,
            country_of_residence="CA",
            nationality="PK",
            intended_visa_type="study",
            intended_destination="CA",
        )
    )
    await session.flush()
    return user


async def _make_assessment(session: AsyncSession, seeker_id: uuid.UUID) -> Assessment:
    assessment = Assessment(
        user_id=seeker_id,
        destination_country="CA",
        visa_type="study",
        status=AssessmentStatus.completed,
        score=72.0,
        completed_at=datetime.now(UTC) - timedelta(days=1),
        created_by=seeker_id,
    )
    session.add(assessment)
    await session.flush()
    return assessment


async def _make_booking(
    session: AsyncSession,
    *,
    seeker_id: uuid.UUID,
    advisor_id: uuid.UUID,
    start: datetime,
    status: BookingStatus,
    payment_status: PaymentStatus,
    service_type: str = "immigration_specialist",
    price_usd: float = 180.0,
) -> Booking:
    booking = Booking(
        seeker_id=seeker_id,
        advisor_id=advisor_id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type=service_type,
        duration_minutes=45,
        price_usd=price_usd,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=45),
        status=status,
        payment_status=payment_status,
        created_by=seeker_id,
    )
    session.add(booking)
    await session.flush()
    return booking


async def _make_succeeded_txn(
    session: AsyncSession, booking: Booking, when: datetime
) -> Transaction:
    amount = booking.price_usd
    commission = round(amount * COMMISSION_RATE, 2)
    tax = round(amount * TAX_RATE, 2)
    txn = Transaction(
        booking_id=booking.id,
        stripe_checkout_session_id=f"cs_dash_{uuid.uuid4().hex[:10]}",
        stripe_payment_intent_id=f"pi_dash_{uuid.uuid4().hex[:10]}",
        stripe_charge_id=f"ch_dash_{uuid.uuid4().hex[:10]}",
        amount_usd=amount,
        commission_rate=COMMISSION_RATE,
        commission_usd=commission,
        tax_rate=TAX_RATE,
        tax_usd=tax,
        advisor_payout_usd=round(amount - commission - tax, 2),
        payment_method="card",
        status=TransactionStatus.succeeded,
        created_by=booking.seeker_id,
    )
    txn.created_at = when
    session.add(txn)
    await session.flush()
    return txn


async def _make_review(session: AsyncSession, booking: Booking, advisor_id: uuid.UUID) -> Review:
    review = Review(
        booking_id=booking.id,
        seeker_id=booking.seeker_id,
        advisor_id=advisor_id,
        rating_expertise=5,
        rating_communication=4,
        rating_professionalism=5,
        rating_value=4,
        rating_overall=4.5,
        text="Very helpful session, cleared up my visa questions.",
        is_verified=True,
        advisor_response=None,  # unanswered → drives pending_reviews_count
        moderation_status=ModerationStatus.visible,
        created_by=booking.seeker_id,
    )
    session.add(review)
    await session.flush()
    return review


async def _make_thread(
    session: AsyncSession,
    *,
    seeker: User,
    advisor_id: uuid.UUID,
    last_from: str,  # "seeker" | "advisor" | "none"
    unread: bool,
    when: datetime,
) -> Conversation:
    conversation = Conversation(
        seeker_id=seeker.id,
        advisor_id=advisor_id,
        created_by=seeker.id,
    )
    session.add(conversation)
    await session.flush()

    if last_from == "none":
        return conversation

    # First message always from the seeker (they open the inquiry).
    seeker_msg = Message(
        conversation_id=conversation.id,
        sender_id=seeker.id,
        body="Hi, I'd like help with my study-permit application.",
        created_by=seeker.id,
        created_at=when,
        read_at=None if (last_from == "seeker" and unread) else when + timedelta(minutes=1),
    )
    session.add(seeker_msg)
    conversation.last_message_at = when

    if last_from == "advisor":
        advisor_msg = Message(
            conversation_id=conversation.id,
            sender_id=advisor_id,
            body="Happy to help — could you share your latest transcript?",
            created_by=advisor_id,
            created_at=when + timedelta(minutes=5),
            read_at=None,
        )
        session.add(advisor_msg)
        conversation.last_message_at = when + timedelta(minutes=5)

    session.add(conversation)
    await session.flush()
    return conversation


async def _trim_profile_to_80(session: AsyncSession, profile: AdvisorProfile) -> None:
    """Populate everything except photo + bookable services so completion lands ~80%.

    The completion formula weights ten items to 16 total; filling the other eight
    (13 weight) while leaving photo (1) and services (2) empty yields 13/16 ≈ 81%.
    Deterministic regardless of the advisor's prior profile state — it fills every
    scalar field and ensures one row in each child collection that counts, then
    clears photo + services so the tile stays under 100%.
    """
    profile.bio = "Immigration specialist helping skilled workers relocate."
    profile.years_of_experience = 8
    profile.country_of_residence = "CA"
    profile.expertise_description = "Study permits, work permits, and PR pathways."
    profile.profile_photo_url = None

    # Ensure one row in each collection the completion check rewards (idempotent:
    # only add when empty so re-runs don't accumulate duplicates).
    if not profile.visa_specializations:
        session.add(AdvisorVisaSpecialization(profile_id=profile.id, specialization="study"))
    if not profile.country_expertise:
        session.add(AdvisorCountryExpertise(profile_id=profile.id, country_code="CA"))
    if not profile.languages:
        session.add(
            AdvisorLanguage(profile_id=profile.id, language="English", proficiency="native")
        )
    if not profile.offered_services:
        session.add(
            AdvisorOfferedService(profile_id=profile.id, service_type="immigration_specialist")
        )

    # Leave services empty → tile stays under 100%.
    await session.execute(delete(AdvisorService).where(AdvisorService.profile_id == profile.id))


async def seed_advisor_dashboard(advisor_id: uuid.UUID) -> list[str]:
    lines: list[str] = []
    password_hash = hash_password(PASSWORD)
    now = datetime.now(UTC)

    async with async_session_factory() as session:
        advisor = await session.get(User, advisor_id)
        if advisor is None or advisor.role != UserRole.advisor:
            raise SystemExit(f"Target advisor {advisor_id} not found or is not an advisor")
        profile = (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id == advisor_id)
            )
        ).scalar_one_or_none()
        if profile is None:
            raise SystemExit(f"Advisor {advisor_id} has no advisor_profile")

        cleared = await _clear_prior(session, advisor_id)
        lines.append(f"cleared_prior_seekers={cleared}")

        # Regulatory feed.
        for country_code, region_label, title, description, days_ago in _REGULATORY:
            session.add(
                RegulatoryUpdate(
                    country_code=country_code,
                    region_label=region_label,
                    title=title,
                    description=description,
                    published_at=now - timedelta(days=days_ago),
                )
            )
        lines.append(f"regulatory_updates={len(_REGULATORY)}")

        # next_upcoming — one confirmed booking 3 days out.
        upcoming_seeker = await _make_seeker(
            session, slug="upcoming", full_name="Ava Thompson", password_hash=password_hash
        )
        upcoming = await _make_booking(
            session,
            seeker_id=upcoming_seeker.id,
            advisor_id=advisor_id,
            start=now + timedelta(days=3, hours=2),
            status=BookingStatus.confirmed,
            payment_status=PaymentStatus.paid,
        )
        lines.append(f"next_upcoming appointment=#{upcoming.appointment_number}")

        # total_earned — two succeeded transactions in the last week.
        earned = 0.0
        for slug, name, amount, days_ago in (
            ("earn1", "Noah Williams", 180.0, 2),
            ("earn2", "Emma Garcia", 250.0, 5),
        ):
            seeker = await _make_seeker(
                session, slug=slug, full_name=name, password_hash=password_hash
            )
            booking = await _make_booking(
                session,
                seeker_id=seeker.id,
                advisor_id=advisor_id,
                start=now - timedelta(days=days_ago + 3),
                status=BookingStatus.completed,
                payment_status=PaymentStatus.paid,
                price_usd=amount,
            )
            txn = await _make_succeeded_txn(session, booking, now - timedelta(days=days_ago))
            earned += txn.advisor_payout_usd
        lines.append(f"total_earned_usd~={round(earned, 2)} (last 7 days)")

        # pending_reviews — two unanswered reviews on completed bookings.
        for slug, name in (("rev1", "Liam Brown"), ("rev2", "Olivia Davis")):
            seeker = await _make_seeker(
                session, slug=slug, full_name=name, password_hash=password_hash
            )
            booking = await _make_booking(
                session,
                seeker_id=seeker.id,
                advisor_id=advisor_id,
                start=now - timedelta(days=10),
                status=BookingStatus.completed,
                payment_status=PaymentStatus.paid,
            )
            await _make_review(session, booking, advisor_id)
        lines.append("pending_reviews_count=2 (unanswered)")

        # new_leads — three AI-matched leads in ``new`` status.
        for i, (slug, name) in enumerate(
            (("lead1", "Sophia Martinez"), ("lead2", "Mason Lee"), ("lead3", "Isabella Chen"))
        ):
            seeker = await _make_seeker(
                session, slug=slug, full_name=name, password_hash=password_hash
            )
            assessment = await _make_assessment(session, seeker.id)
            session.add(
                AdvisorLead(
                    seeker_id=seeker.id,
                    advisor_id=advisor_id,
                    assessment_id=assessment.id,
                    match_score=90.0 - i * 5,
                    match_reasons="Specializes in CA immigration; 8 years of experience",
                    status=AdvisorLeadStatus.new,
                )
            )
        lines.append("new_leads_count=3 (status=new)")

        # client_inquiries — new / unread / responded threads.
        for slug, name, last_from, unread in (
            ("inq_new", "Ethan Wilson", "none", False),
            ("inq_unread", "Mia Anderson", "seeker", True),
            ("inq_responded", "Lucas Taylor", "advisor", False),
        ):
            seeker = await _make_seeker(
                session, slug=slug, full_name=name, password_hash=password_hash
            )
            await _make_thread(
                session,
                seeker=seeker,
                advisor_id=advisor_id,
                last_from=last_from,
                unread=unread,
                when=now - timedelta(hours=6),
            )
        lines.append("client_inquiries=3 (new / unread / responded)")

        # profile_completion — populate to ~80% (photo + services left empty).
        await _trim_profile_to_80(session, profile)
        session.add(profile)
        await session.flush()
        lines.append("profile_completion_percent~=80 (photo + services dropped)")

        await session.commit()

    lines.append(f"advisor_user_id={advisor_id}")
    lines.append(f"seeker_password={PASSWORD}")
    return lines


async def main() -> None:
    raw = os.environ.get("ADVISOR_USER_ID")
    advisor_id = uuid.UUID(raw) if raw else DEFAULT_ADVISOR_USER_ID
    try:
        for line in await seed_advisor_dashboard(advisor_id):
            print(line)
        print()
        print("GET  /api/v1/advisors/me/dashboard?days=7|30|90")
        print("GET  /api/v1/advisors/me/dashboard/export?days=30")
        print("GET  /api/v1/advisors/me/regulatory-updates?days=30")
        print("GET  /api/v1/advisors/me/regulatory-updates/{id}")
        print("GET  /api/v1/conversations?q=Mia")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
