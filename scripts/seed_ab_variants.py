"""Seed A/B test variants for the AI Engine admin panel.

Populates ``GET /api/v1/admin/ab-variants`` with cards that include
``id``, ``label``, ``question``, and ``conversion_rate``::

    uv run python -m scripts.seed_ab_variants

Idempotent: upserts by label (A/B/C/D). Also attaches sample assessments so
conversion rates are non-zero.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.assessment import Assessment, AssessmentStatus, EligibilityTier
from app.models.assessment_ab_variant import AssessmentAbVariant
from app.models.user import User, UserRole
from app.schemas.ab_variant import AbVariantCreate, AbVariantUpdate
from app.services import ab_variant_service

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
SEEKER_EMAIL = "ab.variant.seeker@globlejump.test"

# label, name, question, description, started, completed
SEED_VARIANTS: list[tuple[str, str, str, str, int, int]] = [
    (
        "A",
        "Control — short form",
        "Does a shorter eligibility questionnaire improve completion?",
        "Baseline 8-question flow.",
        120,
        78,
    ),
    (
        "B",
        "Variant — progressive disclosure",
        "Does revealing questions one-at-a-time raise conversion?",
        "Same content, stepped UI.",
        118,
        91,
    ),
    (
        "C",
        "Variant — visa-first framing",
        "Does asking destination visa type first change pass rates?",
        "Reordered opening questions.",
        95,
        52,
    ),
    (
        "D",
        "Variant — confidence copy",
        "Does encouraging copy on hard questions reduce drop-off?",
        "Softened wording on financial/criminal items.",
        88,
        41,
    ),
]


async def _ensure_seeker(session) -> User:
    user = await session.scalar(select(User).where(User.email == SEEKER_EMAIL))
    if user is not None:
        return user
    user = User(
        email=SEEKER_EMAIL,
        full_name="A/B Variant Seed Seeker",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    return user


async def _admin_id(session) -> uuid.UUID:
    admin = await session.scalar(select(User).where(User.role == UserRole.admin).limit(1))
    return admin.id if admin is not None else uuid.uuid4()


async def _upsert_variant(
    session, admin_id: uuid.UUID, data: tuple[str, str, str, str, int, int]
) -> AssessmentAbVariant:
    label, name, question, description, _started, _completed = data
    existing = await session.scalar(
        select(AssessmentAbVariant).where(AssessmentAbVariant.label == label.upper())
    )
    if existing is None:
        return await ab_variant_service.create(
            session,
            AbVariantCreate(
                label=label,
                name=name,
                question=question,
                description=description,
                is_active=True,
            ),
            admin_id,
        )
    return await ab_variant_service.update(
        session,
        existing,
        AbVariantUpdate(
            name=name,
            question=question,
            description=description,
            is_active=True,
        ),
        admin_id,
    )


async def _seed_traffic(
    session,
    *,
    seeker_id: uuid.UUID,
    variant: AssessmentAbVariant,
    started: int,
    completed: int,
) -> None:
    await session.execute(delete(Assessment).where(Assessment.ab_variant_id == variant.id))
    await session.flush()
    now = datetime.now(UTC)
    for i in range(started):
        is_done = i < completed
        created = now - timedelta(days=(i % 40), hours=i % 12)
        assessment = Assessment(
            user_id=seeker_id,
            destination_country="CA",
            visa_type="student",
            status=AssessmentStatus.completed if is_done else AssessmentStatus.in_progress,
            score=75.0 if is_done else None,
            tier=EligibilityTier.likely_eligible if is_done else None,
            confidence=0.8 if is_done else None,
            completed_at=created + timedelta(minutes=10) if is_done else None,
            ab_variant_id=variant.id,
            created_by=seeker_id,
        )
        assessment.created_at = created
        session.add(assessment)


async def seed_ab_variants() -> list[str]:
    lines: list[str] = []
    async with async_session_factory() as session:
        admin_id = await _admin_id(session)
        seeker = await _ensure_seeker(session)
        for data in SEED_VARIANTS:
            label, _name, _q, _d, started, completed = data
            variant = await _upsert_variant(session, admin_id, data)
            await _seed_traffic(
                session,
                seeker_id=seeker.id,
                variant=variant,
                started=started,
                completed=completed,
            )
            rate = round(100 * completed / started, 1) if started else 0.0
            lines.append(f"{label}: conversion≈{rate}% ({completed}/{started})")
            logger.info("ab_variant_seeded", label=label, id=str(variant.id))
        await session.commit()
    return lines


async def main() -> None:
    try:
        for line in await seed_ab_variants():
            print(line)
        print()
        print("List: GET /api/v1/admin/ab-variants")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
