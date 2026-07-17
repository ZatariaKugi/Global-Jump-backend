"""Seed 5 fully-onboarded visa-seeker accounts for local testing.

Each seeker gets: a verified/active User account and a complete SeekerProfile
(identity, intended visa/destination, encrypted passport, travel history,
prior visas, education/employment background, financial band) — mirroring
what a real seeker looks like after finishing the onboarding wizard
(POST /users/me/onboarding).

Run with:
    uv run python -m scripts.seed_seekers

Idempotent: re-running skips seekers whose email already exists.
All accounts share the password printed at the end (or set via
SEED_SEEKER_PASSWORD).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.encryption import encrypt_field
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.seeker_profile import (
    EducationLevel,
    EmploymentStatus,
    SeekerCountryVisited,
    SeekerPriorVisa,
    SeekerProfile,
)
from app.models.user import User, UserRole, VerificationStatus

logger = get_logger(__name__)

DEFAULT_PASSWORD = "TestPass123!"

SEEKERS = [
    {
        "email": "seeker1.seed@globlejump.test",
        "full_name": "Ahmed Farooq",
        "date_of_birth": date(1994, 3, 12),
        "nationality": "PK",
        "country_of_residence": "PK",
        "intended_visa_type": "work",
        "intended_destination": "CA",
        "passport_number": "PK1234567",
        "passport_expiry": date(2029, 6, 1),
        "countries_visited": ["AE", "GB"],
        "prior_visas": [("AE", "work", 2019)],
        "education_level": EducationLevel.bachelor,
        "employment_status": EmploymentStatus.employed,
        "employer_name": "Nexlogic Systems",
        "annual_income_band": "50k_75k",
        "has_bank_statements": True,
    },
    {
        "email": "seeker2.seed@globlejump.test",
        "full_name": "Mei Lin",
        "date_of_birth": date(1998, 11, 2),
        "nationality": "CN",
        "country_of_residence": "CN",
        "intended_visa_type": "student",
        "intended_destination": "US",
        "passport_number": "CN9876543",
        "passport_expiry": date(2028, 2, 15),
        "countries_visited": ["JP", "KR"],
        "prior_visas": [],
        "education_level": EducationLevel.master,
        "employment_status": EmploymentStatus.student,
        "employer_name": None,
        "annual_income_band": "0_25k",
        "has_bank_statements": True,
    },
    {
        "email": "seeker3.seed@globlejump.test",
        "full_name": "Carlos Mendoza",
        "date_of_birth": date(1990, 7, 22),
        "nationality": "MX",
        "country_of_residence": "MX",
        "intended_visa_type": "pr",
        "intended_destination": "GB",
        "passport_number": "MX5551234",
        "passport_expiry": date(2027, 9, 30),
        "countries_visited": ["US", "ES", "FR"],
        "prior_visas": [("US", "tourist", 2016), ("ES", "tourist", 2021)],
        "education_level": EducationLevel.bachelor,
        "employment_status": EmploymentStatus.self_employed,
        "employer_name": "Mendoza Consulting",
        "annual_income_band": "75k_100k",
        "has_bank_statements": True,
    },
    {
        "email": "seeker4.seed@globlejump.test",
        "full_name": "Fatima Al-Sayed",
        "date_of_birth": date(2001, 1, 18),
        "nationality": "EG",
        "country_of_residence": "EG",
        "intended_visa_type": "tourist",
        "intended_destination": "AU",
        "passport_number": "EG7418529",
        "passport_expiry": date(2030, 4, 10),
        "countries_visited": [],
        "prior_visas": [],
        "education_level": EducationLevel.high_school,
        "employment_status": EmploymentStatus.unemployed,
        "employer_name": None,
        "annual_income_band": "0_25k",
        "has_bank_statements": False,
    },
    {
        "email": "seeker5.seed@globlejump.test",
        "full_name": "Daniel Osei",
        "date_of_birth": date(1985, 5, 30),
        "nationality": "GH",
        "country_of_residence": "GH",
        "intended_visa_type": "family",
        "intended_destination": "CA",
        "passport_number": "GH3692581",
        "passport_expiry": date(2026, 12, 20),
        "countries_visited": ["NG", "GB", "CA"],
        "prior_visas": [("CA", "tourist", 2018)],
        "education_level": EducationLevel.phd,
        "employment_status": EmploymentStatus.employed,
        "employer_name": "Osei & Partners",
        "annual_income_band": "100k_plus",
        "has_bank_statements": True,
    },
]


async def _seed_one(
    session: AsyncSession, data: dict[str, Any], password_hash: str, settings: Any
) -> None:
    existing = await session.scalar(select(User).where(User.email == data["email"]))
    if existing is not None:
        logger.info("seeker_seed_skipped_exists", email=data["email"])
        return

    user = User(
        email=data["email"],
        full_name=data["full_name"],
        hashed_password=password_hash,
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC),
        verification_status=VerificationStatus.approved,
    )
    session.add(user)
    await session.flush()

    profile = SeekerProfile(
        user_id=user.id,
        date_of_birth=data["date_of_birth"],
        nationality=data["nationality"],
        country_of_residence=data["country_of_residence"],
        intended_visa_type=data["intended_visa_type"],
        intended_destination=data["intended_destination"],
        passport_number_encrypted=encrypt_field(data["passport_number"], settings),
        passport_expiry=data["passport_expiry"],
        education_level=data["education_level"],
        employment_status=data["employment_status"],
        employer_name=data["employer_name"],
        annual_income_band=data["annual_income_band"],
        has_bank_statements=data["has_bank_statements"],
    )
    session.add(profile)
    await session.flush()

    for code in data["countries_visited"]:
        session.add(SeekerCountryVisited(profile_id=profile.id, country_code=code))
    for country, visa_type, year in data["prior_visas"]:
        session.add(
            SeekerPriorVisa(profile_id=profile.id, country=country, visa_type=visa_type, year=year)
        )

    await session.flush()
    logger.info("seeker_seeded", email=data["email"])


async def seed_seekers(password: str) -> None:
    settings = get_settings()
    password_hash = hash_password(password)
    async with async_session_factory() as session:
        for data in SEEKERS:
            await _seed_one(session, data, password_hash, settings)
        await session.commit()


async def main() -> None:
    password = os.environ.get("SEED_SEEKER_PASSWORD", DEFAULT_PASSWORD)
    try:
        await seed_seekers(password)
    finally:
        await engine.dispose()
    print(f"Done. All seeded seekers share the password: {password}")


if __name__ == "__main__":
    asyncio.run(main())
