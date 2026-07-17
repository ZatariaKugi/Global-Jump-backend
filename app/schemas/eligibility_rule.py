"""Schemas for admin-configurable eligibility scoring rules (PRD §3.4)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.core.visa_types import OptionalVisaType
from app.models.eligibility_rule import EligibilityRuleCategory


class EligibilityRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    category: EligibilityRuleCategory = EligibilityRuleCategory.other
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    points: float = Field(ge=0, le=100)
    weightage_pct: float = Field(ge=0, le=100)
    is_active: bool = True


class EligibilityRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    category: EligibilityRuleCategory | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    points: float | None = Field(default=None, ge=0, le=100)
    weightage_pct: float | None = Field(default=None, ge=0, le=100)
    is_active: bool | None = None


class EligibilityRuleRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    category: EligibilityRuleCategory
    country_code: str | None
    visa_type: OptionalVisaType
    points: float
    weightage_pct: float
    is_active: bool
