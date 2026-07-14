"""Schemas for admin-configurable eligibility scoring rules (PRD §3.4)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class EligibilityRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: str | None = Field(default=None, max_length=50)
    points: float = Field(ge=0, le=100)
    weightage_pct: float = Field(ge=0, le=100)
    is_active: bool = True


class EligibilityRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: str | None = Field(default=None, max_length=50)
    points: float | None = Field(default=None, ge=0, le=100)
    weightage_pct: float | None = Field(default=None, ge=0, le=100)
    is_active: bool | None = None


class EligibilityRuleRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    country_code: str | None
    visa_type: str | None
    points: float
    weightage_pct: float
    is_active: bool
