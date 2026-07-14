"""Seed 5 fully-onboarded, verified advisor accounts for local testing.

Each advisor gets: a verified/active User account, a complete AdvisorProfile
(title, bio, specializations, country expertise, languages, service
offerings), one verified credential document, and a weekly availability
schedule — mirroring what a real advisor would have after finishing the
onboarding wizard and passing admin verification.

Run with:
    uv run python -m scripts.seed_advisors

Idempotent: re-running skips advisors whose email already exists.
All accounts share the password printed at the end (or set via
SEED_ADVISOR_PASSWORD).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import select
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
from app.models.user import User, UserRole, VerificationStatus
from app.services.advisor_search_service import generate_unique_slug

logger = get_logger(__name__)

DEFAULT_PASSWORD = "TestPass123!"

ADVISORS = [
    {
        "email": "advisor1.seed@globlejump.test",
        "full_name": "Sarah Mitchell",
        "title": "Canadian Immigration Consultant",
        "bio": "10+ years helping clients navigate Express Entry, PNP, and study permits.",
        "years_of_experience": 11,
        "successful_applications": 340,
        "specializations": ["work", "student", "permanent_residency"],
        "countries": ["CA", "US"],
        "languages": [("English", "native"), ("French", "fluent")],
        "services": [
            ("consultation", 30, 49.0),
            ("full_application_review", 90, 249.0),
        ],
    },
    {
        "email": "advisor2.seed@globlejump.test",
        "full_name": "James Okafor",
        "title": "US Immigration Attorney",
        "bio": "Specializes in H-1B, O-1, and family-based petitions.",
        "years_of_experience": 8,
        "successful_applications": 210,
        "specializations": ["work", "family"],
        "countries": ["US"],
        "languages": [("English", "native")],
        "services": [
            ("consultation", 30, 75.0),
            ("case_strategy_session", 60, 150.0),
        ],
    },
    {
        "email": "advisor3.seed@globlejump.test",
        "full_name": "Priya Nair",
        "title": "UK Visa & Settlement Advisor",
        "bio": "Skilled Worker, Student, and Family visa specialist registered with OISC.",
        "years_of_experience": 6,
        "successful_applications": 150,
        "specializations": ["work", "student", "family"],
        "countries": ["GB"],
        "languages": [("English", "native"), ("Hindi", "fluent")],
        "services": [
            ("consultation", 30, 40.0),
            ("document_review", 45, 90.0),
        ],
    },
    {
        "email": "advisor4.seed@globlejump.test",
        "full_name": "Liam Bennett",
        "title": "Australian Migration Agent",
        "bio": "MARA-registered agent focused on skilled migration and partner visas.",
        "years_of_experience": 13,
        "successful_applications": 400,
        "specializations": ["work", "permanent_residency", "family"],
        "countries": ["AU"],
        "languages": [("English", "native")],
        "services": [
            ("consultation", 30, 60.0),
            ("full_application_review", 90, 280.0),
        ],
    },
    {
        "email": "advisor5.seed@globlejump.test",
        "full_name": "Elena Rossi",
        "title": "Multi-Country Investment & Study Visa Advisor",
        "bio": "Helps clients with investment visas and study pathways across CA, UK, and AU.",
        "years_of_experience": 9,
        "successful_applications": 175,
        "specializations": ["investment", "student"],
        "countries": ["CA", "GB", "AU"],
        "languages": [("English", "native"), ("Italian", "fluent")],
        "services": [
            ("consultation", 30, 55.0),
            ("case_strategy_session", 60, 160.0),
        ],
    },
]

# Mon–Fri, 09:00–17:00 in the advisor's local timezone
WEEKLY_SLOTS = [
    (weekday, time(9, 0), time(17, 0), "America/Toronto") for weekday in range(5)
]


async def _seed_one(session: AsyncSession, data: dict[str, Any], password_hash: str) -> None:
    existing = await session.scalar(select(User).where(User.email == data["email"]))
    if existing is not None:
        logger.info("advisor_seed_skipped_exists", email=data["email"])
        return

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


async def seed_advisors(password: str) -> None:
    password_hash = hash_password(password)
    async with async_session_factory() as session:
        for data in ADVISORS:
            await _seed_one(session, data, password_hash)
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
