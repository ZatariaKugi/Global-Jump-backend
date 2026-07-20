"""A/B test variant CRUD and conversion stats."""

from __future__ import annotations

import random
import uuid

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.assessment import Assessment, AssessmentStatus
from app.models.assessment_ab_variant import AssessmentAbVariant
from app.schemas.ab_variant import AbVariantCreate, AbVariantRead, AbVariantUpdate


def list_stmt(
    country: str | None = None, visa_type: str | None = None
) -> Select[tuple[AssessmentAbVariant]]:
    stmt = select(AssessmentAbVariant).order_by(AssessmentAbVariant.label)
    if country:
        stmt = stmt.where(
            or_(
                AssessmentAbVariant.country_code.is_(None),
                AssessmentAbVariant.country_code == country.upper(),
            )
        )
    if visa_type:
        stmt = stmt.where(
            or_(
                AssessmentAbVariant.visa_type.is_(None),
                AssessmentAbVariant.visa_type == visa_type.lower(),
            )
        )
    return stmt


async def create(
    session: AsyncSession, data: AbVariantCreate, admin_id: uuid.UUID
) -> AssessmentAbVariant:
    row = AssessmentAbVariant(
        label=data.label.upper(),
        name=data.name,
        question=data.question,
        description=data.description,
        country_code=data.country_code.upper() if data.country_code else None,
        visa_type=data.visa_type.lower() if data.visa_type else None,
        is_active=data.is_active,
        created_by=admin_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def update(
    session: AsyncSession,
    row: AssessmentAbVariant,
    data: AbVariantUpdate,
    admin_id: uuid.UUID,
) -> AssessmentAbVariant:
    fields = data.model_dump(exclude_unset=True)
    if "label" in fields and fields["label"] is not None:
        fields["label"] = str(fields["label"]).upper()
    if "country_code" in fields:
        code = fields.pop("country_code")
        row.country_code = code.upper() if code else None
    if "visa_type" in fields:
        vt = fields.pop("visa_type")
        row.visa_type = vt.lower() if vt else None
    for field, value in fields.items():
        setattr(row, field, value)
    row.updated_by = admin_id
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def delete(session: AsyncSession, row: AssessmentAbVariant) -> None:
    await session.delete(row)
    await session.flush()


async def get_by_id(session: AsyncSession, variant_id: uuid.UUID) -> AssessmentAbVariant:
    row = await session.get(AssessmentAbVariant, variant_id)
    if row is None:
        raise NotFoundError("A/B variant not found")
    return row


async def pick_for_scope(
    session: AsyncSession, country: str, visa_type: str
) -> AssessmentAbVariant | None:
    """Random active variant matching the assessment scope (or global)."""
    result = await session.execute(
        select(AssessmentAbVariant)
        .where(AssessmentAbVariant.is_active.is_(True))
        .where(
            or_(
                AssessmentAbVariant.country_code.is_(None),
                AssessmentAbVariant.country_code == country.upper(),
            )
        )
        .where(
            or_(
                AssessmentAbVariant.visa_type.is_(None),
                AssessmentAbVariant.visa_type == visa_type.lower(),
            )
        )
    )
    variants = list(result.scalars().all())
    if not variants:
        return None
    return random.choice(variants)


async def build_reads(
    session: AsyncSession, variants: list[AssessmentAbVariant]
) -> list[AbVariantRead]:
    if not variants:
        return []
    ids = [v.id for v in variants]
    started_rows = (
        await session.execute(
            select(Assessment.ab_variant_id, func.count(Assessment.id))
            .where(Assessment.ab_variant_id.in_(ids))
            .group_by(Assessment.ab_variant_id)
        )
    ).all()
    completed_rows = (
        await session.execute(
            select(Assessment.ab_variant_id, func.count(Assessment.id))
            .where(Assessment.ab_variant_id.in_(ids))
            .where(Assessment.status == AssessmentStatus.completed)
            .group_by(Assessment.ab_variant_id)
        )
    ).all()
    started = {vid: int(c) for vid, c in started_rows}
    completed = {vid: int(c) for vid, c in completed_rows}

    out: list[AbVariantRead] = []
    for v in variants:
        s = started.get(v.id, 0)
        c = completed.get(v.id, 0)
        rate = round(100 * c / s, 1) if s else 0.0
        out.append(
            AbVariantRead(
                id=v.id,
                label=v.label,
                name=v.name,
                question=v.question,
                description=v.description,
                country_code=v.country_code,
                visa_type=v.visa_type,
                is_active=v.is_active,
                started_count=s,
                completed_count=c,
                conversion_rate=rate,
            )
        )
    return out
