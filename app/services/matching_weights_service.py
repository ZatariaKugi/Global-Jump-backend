"""Load / upsert advisor matching weight config."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.advisor_matching_weights import AdvisorMatchingWeights
from app.schemas.matching_weights import MatchingWeightsRead, MatchingWeightsUpdate

DEFAULT_COUNTRY = 25.0
DEFAULT_VISA = 25.0
DEFAULT_LANGUAGE = 15.0
DEFAULT_SERVICES = 15.0
DEFAULT_EXPERIENCE = 10.0
DEFAULT_RATING = 10.0


@dataclass(frozen=True)
class MatchingWeightConfig:
    country: float
    visa: float
    language: float
    services: float
    experience: float
    rating: float


DEFAULT_CONFIG = MatchingWeightConfig(
    country=DEFAULT_COUNTRY,
    visa=DEFAULT_VISA,
    language=DEFAULT_LANGUAGE,
    services=DEFAULT_SERVICES,
    experience=DEFAULT_EXPERIENCE,
    rating=DEFAULT_RATING,
)


async def _singleton(session: AsyncSession) -> AdvisorMatchingWeights | None:
    """Return the weights row, collapsing duplicates so only one remains."""
    rows = list((await session.execute(select(AdvisorMatchingWeights))).scalars().all())
    if not rows:
        return None
    if len(rows) > 1:
        for extra in rows[1:]:
            await session.delete(extra)
        await session.flush()
    return rows[0]


def _from_row(row: AdvisorMatchingWeights) -> MatchingWeightConfig:
    return MatchingWeightConfig(
        country=row.country_weight,
        visa=row.visa_weight,
        language=row.language_weight,
        services=row.services_weight,
        experience=row.experience_weight,
        rating=row.rating_weight,
    )


def _to_read(row: AdvisorMatchingWeights | None) -> MatchingWeightsRead:
    if row is None:
        return MatchingWeightsRead(
            id=None,
            country_weight=DEFAULT_COUNTRY,
            visa_weight=DEFAULT_VISA,
            language_weight=DEFAULT_LANGUAGE,
            services_weight=DEFAULT_SERVICES,
            experience_weight=DEFAULT_EXPERIENCE,
            rating_weight=DEFAULT_RATING,
            availability_weight=DEFAULT_SERVICES,
            setting_weight=DEFAULT_VISA,
        )
    return MatchingWeightsRead(
        id=row.id,
        country_weight=row.country_weight,
        visa_weight=row.visa_weight,
        language_weight=row.language_weight,
        services_weight=row.services_weight,
        experience_weight=row.experience_weight,
        rating_weight=row.rating_weight,
        availability_weight=row.services_weight,
        setting_weight=row.visa_weight,
    )


async def get_config(session: AsyncSession) -> MatchingWeightConfig:
    row = (await session.execute(select(AdvisorMatchingWeights).limit(1))).scalar_one_or_none()
    if row is None:
        return DEFAULT_CONFIG
    return _from_row(row)


async def get_read(session: AsyncSession) -> MatchingWeightsRead:
    row = (await session.execute(select(AdvisorMatchingWeights).limit(1))).scalar_one_or_none()
    return _to_read(row)


async def upsert(
    session: AsyncSession, data: MatchingWeightsUpdate, admin_id: uuid.UUID
) -> MatchingWeightsRead:
    assert data.visa_weight is not None
    assert data.services_weight is not None
    assert data.experience_weight is not None
    assert data.rating_weight is not None

    row = await _singleton(session)
    if row is not None:
        row.country_weight = data.country_weight
        row.visa_weight = data.visa_weight
        row.language_weight = data.language_weight
        row.services_weight = data.services_weight
        row.experience_weight = data.experience_weight
        row.rating_weight = data.rating_weight
        row.updated_by = admin_id
    else:
        row = AdvisorMatchingWeights(
            country_weight=data.country_weight,
            visa_weight=data.visa_weight,
            language_weight=data.language_weight,
            services_weight=data.services_weight,
            experience_weight=data.experience_weight,
            rating_weight=data.rating_weight,
            created_by=admin_id,
        )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return _to_read(row)
