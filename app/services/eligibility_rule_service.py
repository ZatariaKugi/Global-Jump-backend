"""Admin CRUD for eligibility scoring rules — mirrors the assessment-question
admin CRUD in ``assessment_service`` (create/list/update/delete)."""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eligibility_rule import EligibilityRule
from app.schemas.eligibility_rule import EligibilityRuleCreate, EligibilityRuleUpdate


async def create(
    session: AsyncSession, data: EligibilityRuleCreate, admin_id: uuid.UUID
) -> EligibilityRule:
    rule = EligibilityRule(
        name=data.name,
        description=data.description,
        category=data.category,
        country_code=data.country_code.upper() if data.country_code else None,
        visa_type=data.visa_type.lower() if data.visa_type else None,
        points=data.points,
        weightage_pct=data.weightage_pct,
        is_active=data.is_active,
        created_by=admin_id,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


def list_stmt(
    country: str | None = None, visa_type: str | None = None
) -> Select[tuple[EligibilityRule]]:
    stmt = select(EligibilityRule).order_by(EligibilityRule.created_at)
    if country:
        stmt = stmt.where(EligibilityRule.country_code == country.upper())
    if visa_type:
        stmt = stmt.where(EligibilityRule.visa_type == visa_type.lower())
    return stmt


async def update(
    session: AsyncSession,
    rule: EligibilityRule,
    data: EligibilityRuleUpdate,
    admin_id: uuid.UUID,
) -> EligibilityRule:
    fields = data.model_dump(exclude_unset=True)
    if "country_code" in fields:
        code = fields.pop("country_code")
        rule.country_code = code.upper() if code else None
    if "visa_type" in fields:
        vt = fields.pop("visa_type")
        rule.visa_type = vt.lower() if vt else None
    for field, value in fields.items():
        setattr(rule, field, value)
    rule.updated_by = admin_id
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def delete(session: AsyncSession, rule: EligibilityRule) -> None:
    await session.delete(rule)
    await session.flush()
