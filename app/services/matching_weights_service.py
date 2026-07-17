"""Load / upsert advisor matching weight config."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.advisor_matching_weights import AdvisorMatchingWeights
from app.schemas.matching_weights import MatchingWeightsRead, MatchingWeightsUpdate

DEFAULT_COUNTRY = 40.0
DEFAULT_LANGUAGE = 20.0
DEFAULT_AVAILABILITY = 20.0
DEFAULT_SETTING = 20.0


@dataclass(frozen=True)
class MatchingWeightConfig:
    country: float
    language: float
    availability: float
    setting: float


DEFAULT_CONFIG = MatchingWeightConfig(
    country=DEFAULT_COUNTRY,
    language=DEFAULT_LANGUAGE,
    availability=DEFAULT_AVAILABILITY,
    setting=DEFAULT_SETTING,
)


async def get_config(session: AsyncSession) -> MatchingWeightConfig:
    row = (
        await session.execute(select(AdvisorMatchingWeights).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return DEFAULT_CONFIG
    return MatchingWeightConfig(
        country=row.country_weight,
        language=row.language_weight,
        availability=row.availability_weight,
        setting=row.setting_weight,
    )


async def get_read(session: AsyncSession) -> MatchingWeightsRead:
    row = (
        await session.execute(select(AdvisorMatchingWeights).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return MatchingWeightsRead(
            id=None,
            country_weight=DEFAULT_COUNTRY,
            language_weight=DEFAULT_LANGUAGE,
            availability_weight=DEFAULT_AVAILABILITY,
            setting_weight=DEFAULT_SETTING,
        )
    return MatchingWeightsRead(
        id=row.id,
        country_weight=row.country_weight,
        language_weight=row.language_weight,
        availability_weight=row.availability_weight,
        setting_weight=row.setting_weight,
    )


async def upsert(
    session: AsyncSession, data: MatchingWeightsUpdate, admin_id: uuid.UUID
) -> MatchingWeightsRead:
    row = (
        await session.execute(select(AdvisorMatchingWeights).limit(1))
    ).scalar_one_or_none()
    if row is None:
        row = AdvisorMatchingWeights(
            country_weight=data.country_weight,
            language_weight=data.language_weight,
            availability_weight=data.availability_weight,
            setting_weight=data.setting_weight,
            created_by=admin_id,
        )
    else:
        row.country_weight = data.country_weight
        row.language_weight = data.language_weight
        row.availability_weight = data.availability_weight
        row.setting_weight = data.setting_weight
        row.updated_by = admin_id
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return MatchingWeightsRead(
        id=row.id,
        country_weight=row.country_weight,
        language_weight=row.language_weight,
        availability_weight=row.availability_weight,
        setting_weight=row.setting_weight,
    )
