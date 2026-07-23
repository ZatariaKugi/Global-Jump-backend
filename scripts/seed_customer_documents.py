"""Seed advisor customer-documents list + review UI data.

Creates 3 seekers with bookings against a target advisor and mixed seeker
documents (passport / educational / finance / supporting) in
under_review / approved / rejected statuses, plus a couple of review comments.

    uv run python -m scripts.seed_customer_documents
    uv run python -m scripts.seed_customer_documents \\
        --advisor-id da37a676-127b-48a3-8fde-707d7e9df438

Idempotent for the seed seeker emails: clears prior seed bookings/docs for
those seekers (scoped to the target advisor) and recreates them.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_profile import AdvisorProfile, AdvisorServiceType
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.seeker_document import (
    DocumentCategory,
    SeekerDocument,
    SeekerDocumentComment,
    SeekerDocumentStatus,
)
from app.models.seeker_profile import SeekerProfile
from app.models.user import User, UserRole, VerificationStatus
from app.services import booking_service

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
DEFAULT_ADVISOR_ID = uuid.UUID("da37a676-127b-48a3-8fde-707d7e9df438")

DocSpec = tuple[DocumentCategory, str, SeekerDocumentStatus]
SeekerSpec = tuple[str, str, str, str, BookingStatus, list[DocSpec]]

# (email, full_name, intended_visa, destination, booking_status, docs)
SEEKER_SPECS: list[SeekerSpec] = [
    (
        "docs.seeker1@globlejump.test",
        "Arone Jhon",
        "student",
        "CA",
        BookingStatus.confirmed,
        [
            (DocumentCategory.passport, "Passport Bio Page.pdf", SeekerDocumentStatus.under_review),
            (DocumentCategory.educational, "Bachelor Degree.pdf", SeekerDocumentStatus.approved),
            (DocumentCategory.educational, "Transcript.pdf", SeekerDocumentStatus.under_review),
            (DocumentCategory.finance, "Bank Statement.pdf", SeekerDocumentStatus.rejected),
            (DocumentCategory.supporting, "SOP Draft.pdf", SeekerDocumentStatus.under_review),
        ],
    ),
    (
        "docs.seeker2@globlejump.test",
        "Maya Chen",
        "work",
        "US",
        BookingStatus.pending,
        [
            (DocumentCategory.passport, "US Passport.pdf", SeekerDocumentStatus.approved),
            (DocumentCategory.educational, "Masters Diploma.pdf", SeekerDocumentStatus.approved),
            (DocumentCategory.finance, "Pay Stubs.pdf", SeekerDocumentStatus.approved),
            (DocumentCategory.supporting, "Employment Letter.pdf", SeekerDocumentStatus.approved),
        ],
    ),
    (
        "docs.seeker3@globlejump.test",
        "Omar Hassan",
        "family",
        "GB",
        BookingStatus.confirmed,
        [
            (DocumentCategory.passport, "Passport Scan.pdf", SeekerDocumentStatus.under_review),
            (DocumentCategory.finance, "Sponsor Affidavit.pdf", SeekerDocumentStatus.under_review),
            (
                DocumentCategory.supporting,
                "Marriage Certificate.pdf",
                SeekerDocumentStatus.rejected,
            ),
        ],
    ),
]


async def _ensure_advisor(session: AsyncSession, advisor_id: uuid.UUID) -> User:
    advisor = await session.get(User, advisor_id)
    if advisor is None:
        raise SystemExit(f"Advisor not found: {advisor_id}")
    if advisor.role != UserRole.advisor:
        raise SystemExit(f"User {advisor_id} is role={advisor.role}, expected advisor")
    advisor.is_active = True
    advisor.verification_status = VerificationStatus.approved
    session.add(advisor)

    profile = await session.scalar(
        select(AdvisorProfile).where(AdvisorProfile.user_id == advisor.id)
    )
    if profile is None:
        session.add(
            AdvisorProfile(
                user_id=advisor.id,
                title="Immigration Consultant",
                years_of_experience=5,
            )
        )
        await session.flush()
    return advisor


async def _ensure_seeker(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    visa: str,
    destination: str,
) -> User:
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
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
        logger.info("customer_docs_seeker_created", email=email, id=str(user.id))
    else:
        user.full_name = full_name
        user.is_active = True
        session.add(user)

    profile = await session.scalar(
        select(SeekerProfile).where(SeekerProfile.user_id == user.id)
    )
    if profile is None:
        session.add(
            SeekerProfile(
                user_id=user.id,
                intended_visa_type=visa,
                intended_destination=destination,
                nationality=destination,
                country_of_residence=destination,
                profile_photo_url=f"/uploads/seekers/{user.id}/avatar.jpg",
            )
        )
    else:
        profile.intended_visa_type = visa
        profile.intended_destination = destination
        if not profile.profile_photo_url:
            profile.profile_photo_url = f"/uploads/seekers/{user.id}/avatar.jpg"
        session.add(profile)
    await session.flush()
    return user


async def _clear_seed_for_seeker(
    session: AsyncSession, seeker_id: uuid.UUID, advisor_id: uuid.UUID
) -> tuple[int, int]:
    doc_ids = list(
        (
            await session.execute(
                select(SeekerDocument.id).where(SeekerDocument.seeker_id == seeker_id)
            )
        )
        .scalars()
        .all()
    )
    comments = 0
    if doc_ids:
        comments = (
            await session.execute(
                delete(SeekerDocumentComment).where(
                    SeekerDocumentComment.document_id.in_(doc_ids)
                )
            )
        ).rowcount or 0
        await session.execute(delete(SeekerDocument).where(SeekerDocument.id.in_(doc_ids)))

    bookings = (
        await session.execute(
            delete(Booking).where(
                Booking.seeker_id == seeker_id,
                Booking.advisor_id == advisor_id,
            )
        )
    ).rowcount or 0
    await session.flush()
    return bookings, len(doc_ids) + comments


async def _add_booking(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    status: BookingStatus,
    hours_ahead: int,
) -> Booking:
    start = datetime.now(UTC) + timedelta(hours=hours_ahead)
    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type=AdvisorServiceType.immigration_specialist.value,
        duration_minutes=45,
        price_usd=99.0,
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=45),
        status=status,
        payment_status=(
            PaymentStatus.paid
            if status in (BookingStatus.confirmed, BookingStatus.completed)
            else PaymentStatus.unpaid
        ),
        is_important=status == BookingStatus.pending,
        created_by=seeker.id,
    )
    session.add(booking)
    await session.flush()
    return booking


async def _add_documents(
    session: AsyncSession,
    *,
    seeker: User,
    advisor: User,
    docs: list[tuple[DocumentCategory, str, SeekerDocumentStatus]],
) -> list[SeekerDocument]:
    created: list[SeekerDocument] = []
    now = datetime.now(UTC)
    for i, (category, name, status) in enumerate(docs):
        doc = SeekerDocument(
            seeker_id=seeker.id,
            category=category,
            document_name=name,
            file_url=f"/uploads/seekers/{seeker.id}/{category.value}_{i}.pdf",
            file_size_bytes=120_000 + i * 8_000,
            content_type="application/pdf",
            status=status,
            created_by=seeker.id,
        )
        if status in (SeekerDocumentStatus.approved, SeekerDocumentStatus.rejected):
            doc.reviewed_at = now - timedelta(hours=2 + i)
            doc.reviewed_by = advisor.id
            doc.updated_by = advisor.id
        session.add(doc)
        await session.flush()
        created.append(doc)

        if status == SeekerDocumentStatus.rejected:
            session.add(
                SeekerDocumentComment(
                    document_id=doc.id,
                    author_id=advisor.id,
                    body="Please re-upload a clearer scan — edges are cut off.",
                    created_by=advisor.id,
                )
            )
        elif status == SeekerDocumentStatus.under_review and category == DocumentCategory.passport:
            session.add(
                SeekerDocumentComment(
                    document_id=doc.id,
                    author_id=advisor.id,
                    body="Received — reviewing passport details today.",
                    created_by=advisor.id,
                )
            )
    await session.flush()
    return created


async def seed_customer_documents(advisor_id: uuid.UUID) -> list[str]:
    lines: list[str] = []
    async with async_session_factory() as session:
        advisor = await _ensure_advisor(session, advisor_id)
        lines.append(f"advisor={advisor.email} id={advisor.id}")

        total_bookings = 0
        total_docs = 0
        for i, (email, name, visa, dest, status, docs) in enumerate(SEEKER_SPECS):
            seeker = await _ensure_seeker(
                session,
                email=email,
                full_name=name,
                visa=visa,
                destination=dest,
            )
            cleared_b, cleared_d = await _clear_seed_for_seeker(
                session, seeker.id, advisor.id
            )
            booking = await _add_booking(
                session,
                seeker=seeker,
                advisor=advisor,
                status=status,
                hours_ahead=24 + i * 48,
            )
            created_docs = await _add_documents(
                session, seeker=seeker, advisor=advisor, docs=docs
            )
            total_bookings += 1
            total_docs += len(created_docs)
            lines.append(
                f"seeker={email} booking={booking.appointment_number} "
                f"status={status.value} docs={len(created_docs)} "
                f"(cleared bookings={cleared_b} docs+comments={cleared_d})"
            )

        await session.commit()
        lines.append(f"total_bookings={total_bookings} total_docs={total_docs}")
        lines.append(f"password={PASSWORD}")
    return lines


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--advisor-id",
        type=uuid.UUID,
        default=DEFAULT_ADVISOR_ID,
        help="Advisor user UUID to attach bookings/documents to",
    )
    args = parser.parse_args()
    try:
        for line in await seed_customer_documents(args.advisor_id):
            print(line)
        print()
        print("List: GET /api/v1/advisors/me/customer-documents")
        print("Review: GET /api/v1/advisors/me/clients/{seeker_id}/documents")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
