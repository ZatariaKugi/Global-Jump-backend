"""Seed advisors waiting on the admin Verification Queue.

Creates advisors with ``verification_status=under_review`` (or ``pending``)
and at least one ``pending`` credential document so they appear on
``GET /api/v1/admin/verification-queue``.

Run with:
    uv run python -m scripts.seed_verification_queue

Idempotent: re-running resets each seed advisor's account + credentials back
to a queue-ready state (pending docs). Shared password: TestPass123! (or
SEED_ADVISOR_PASSWORD).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_credential import AdvisorCredential, CredentialStatus, DocumentType
from app.models.advisor_profile import (
    AdvisorCountryExpertise,
    AdvisorLanguage,
    AdvisorProfile,
    AdvisorService,
    AdvisorVisaSpecialization,
)
from app.models.user import User, UserRole, VerificationStatus
from app.services.advisor_search_service import generate_unique_slug

logger = get_logger(__name__)

DEFAULT_PASSWORD = "TestPass123!"

# document_types → pending AdvisorCredential rows (ai_score uses the three
# required types: government_id, license, certification).
QUEUE_ADVISORS: list[dict[str, Any]] = [
    {
        "email": "queue1.seed@globlejump.test",
        "full_name": "Amira Hassan",
        "title": "Canadian Study Permit Advisor",
        "bio": "Awaiting document review — full package submitted.",
        "country_of_residence": "CA",
        "years_of_experience": 5,
        "successful_applications": 80,
        "specializations": ["student", "work"],
        "countries": ["CA"],
        "languages": [("English", "fluent"), ("Arabic", "native")],
        "services": [("immigration_specialist", 30, 45.0)],
        "verification_status": VerificationStatus.under_review,
        "document_types": [
            DocumentType.government_id,
            DocumentType.license,
            DocumentType.certification,
        ],
        # Stagger submission times so the queue has a clear order.
        "submitted_hours_ago": 48,
    },
    {
        "email": "queue2.seed@globlejump.test",
        "full_name": "Diego Morales",
        "title": "US Family Immigration Consultant",
        "bio": "Needs review — missing certification document.",
        "country_of_residence": "US",
        "years_of_experience": 7,
        "successful_applications": 120,
        "specializations": ["family", "work"],
        "countries": ["US", "MX"],
        "languages": [("English", "fluent"), ("Spanish", "native")],
        "services": [("career_coach", 30, 65.0)],
        "verification_status": VerificationStatus.under_review,
        "document_types": [
            DocumentType.government_id,
            DocumentType.license,
        ],
        "submitted_hours_ago": 24,
    },
    {
        "email": "queue3.seed@globlejump.test",
        "full_name": "Mei Chen",
        "title": "UK Skilled Worker Advisor",
        "bio": "Just submitted — early in the queue.",
        "country_of_residence": "GB",
        "years_of_experience": 4,
        "successful_applications": 55,
        "specializations": ["work", "student"],
        "countries": ["GB"],
        "languages": [("English", "fluent"), ("Mandarin", "native")],
        "services": [("resume_writer", 30, 50.0)],
        "verification_status": VerificationStatus.pending,
        "document_types": [
            DocumentType.government_id,
        ],
        "submitted_hours_ago": 6,
    },
]


async def _ensure_profile(session: AsyncSession, user: User, data: dict[str, Any]) -> None:
    profile = await session.scalar(
        select(AdvisorProfile).where(AdvisorProfile.user_id == user.id)
    )
    if profile is not None:
        return

    slug = await generate_unique_slug(session, data["full_name"])
    profile = AdvisorProfile(
        user_id=user.id,
        title=data["title"],
        bio=data["bio"],
        country_of_residence=data["country_of_residence"],
        years_of_experience=data["years_of_experience"],
        successful_applications=data["successful_applications"],
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
    await session.flush()


async def _reset_pending_credentials(
    session: AsyncSession, user: User, data: dict[str, Any]
) -> None:
    """Replace credentials with a fresh pending set for queue membership."""
    existing = (
        await session.execute(
            select(AdvisorCredential).where(AdvisorCredential.user_id == user.id)
        )
    ).scalars().all()
    for cred in existing:
        await session.delete(cred)
    await session.flush()

    base = datetime.now(UTC) - timedelta(hours=int(data["submitted_hours_ago"]))
    for i, doc_type in enumerate(data["document_types"]):
        submitted_at = base + timedelta(minutes=i * 5)
        cred = AdvisorCredential(
            user_id=user.id,
            document_type=doc_type,
            document_name=f"{data['full_name']} - {doc_type.value}.pdf",
            file_url=f"/uploads/credentials/{user.id}/seed-{doc_type.value}.pdf",
            file_size_bytes=120_000 + i * 10_000,
            status=CredentialStatus.pending,
        )
        # BaseModel sets created_at on flush via server_default; stamp explicitly
        # so queue ordering (MIN created_at) matches the seed story.
        cred.created_at = submitted_at
        cred.updated_at = submitted_at
        session.add(cred)
    await session.flush()


async def _seed_one(
    session: AsyncSession, data: dict[str, Any], password_hash: str
) -> User:
    user = await session.scalar(select(User).where(User.email == data["email"]))
    status: VerificationStatus = data["verification_status"]

    if user is None:
        user = User(
            email=data["email"],
            full_name=data["full_name"],
            hashed_password=password_hash,
            role=UserRole.advisor,
            is_active=False,
            email_verified_at=datetime.now(UTC),
            verification_status=status,
        )
        session.add(user)
        await session.flush()
        logger.info("queue_advisor_created", email=data["email"], status=status.value)
    else:
        user.full_name = data["full_name"]
        user.hashed_password = password_hash
        user.role = UserRole.advisor
        user.is_active = False
        user.is_suspended = False
        user.verification_status = status
        user.pre_suspend_verification_status = None
        session.add(user)
        await session.flush()
        logger.info("queue_advisor_reset", email=data["email"], status=status.value)

    await _ensure_profile(session, user, data)
    await _reset_pending_credentials(session, user, data)
    return user


async def seed_verification_queue(password: str) -> list[str]:
    password_hash = hash_password(password)
    emails: list[str] = []
    async with async_session_factory() as session:
        for data in QUEUE_ADVISORS:
            user = await _seed_one(session, data, password_hash)
            emails.append(user.email)
        await session.commit()
    return emails


async def main() -> None:
    password = os.environ.get("SEED_ADVISOR_PASSWORD", DEFAULT_PASSWORD)
    try:
        emails = await seed_verification_queue(password)
    finally:
        await engine.dispose()
    print("Verification-queue seed advisors:")
    for email in emails:
        print(f"  - {email}")
    print(f"Password: {password}")
    print("They should appear on GET /api/v1/admin/verification-queue")


if __name__ == "__main__":
    asyncio.run(main())
