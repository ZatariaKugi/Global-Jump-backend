"""Seed assessment score thresholds for the AI Engine admin panel.

Populates ``GET /api/v1/admin/assessment-thresholds`` (global when called with
no query params) plus several country/visa scopes for the settings UI::

    uv run python -m scripts.seed_assessment_thresholds

Idempotent via upsert (same country + visa_type updates in place).
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import async_session_factory, engine
from app.models.user import User, UserRole
from app.schemas.assessment_threshold import AssessmentThresholdUpsert
from app.services import assessment_service

logger = get_logger(__name__)

# (country_code | None, visa_type | None, highly, likely, borderline)
# First row is the global default returned by GET with no filters.
SEED_THRESHOLDS: list[tuple[str | None, str | None, float, float, float]] = [
    (None, None, 80.0, 60.0, 40.0),  # global
    ("CA", "student", 85.0, 65.0, 45.0),
    ("CA", "work", 82.0, 62.0, 42.0),
    ("US", "tourist", 75.0, 55.0, 35.0),
    ("US", "work", 88.0, 70.0, 50.0),
    ("GB", "work", 80.0, 60.0, 40.0),
    ("GB", "student", 78.0, 58.0, 38.0),
    ("AU", "pr", 90.0, 75.0, 55.0),
    ("DE", "family", 80.0, 60.0, 40.0),
]


async def _admin_id(session) -> uuid.UUID:
    admin = await session.scalar(
        select(User).where(User.role == UserRole.admin).limit(1)
    )
    if admin is not None:
        return admin.id
    return uuid.uuid4()


async def seed_assessment_thresholds() -> list[str]:
    lines: list[str] = []
    async with async_session_factory() as session:
        admin_id = await _admin_id(session)
        for country, visa, highly, likely, borderline in SEED_THRESHOLDS:
            row = await assessment_service.upsert_threshold(
                session,
                AssessmentThresholdUpsert(
                    country_code=country,
                    visa_type=visa,
                    highly_eligible_min=highly,
                    likely_eligible_min=likely,
                    borderline_min=borderline,
                    is_active=True,
                ),
                admin_id,
            )
            scope = f"{country or 'global'}/{visa or 'all'}"
            lines.append(
                f"{scope} highly={row.highly_eligible_min} "
                f"likely={row.likely_eligible_min} borderline={row.borderline_min}"
            )
            logger.info(
                "threshold_seeded",
                country=country,
                visa_type=visa,
                id=str(row.id),
            )
        await session.commit()
    return lines


async def main() -> None:
    try:
        for line in await seed_assessment_thresholds():
            print(line)
        print()
        print("Global (your curl): GET /api/v1/admin/assessment-thresholds")
        print(
            "Scoped example: GET /api/v1/admin/assessment-thresholds"
            "?country=CA&visa_type=student"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
