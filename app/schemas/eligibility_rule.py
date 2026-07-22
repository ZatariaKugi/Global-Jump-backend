"""Schemas for admin-configurable eligibility scoring rules (PRD §3.4)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.visa_types import OptionalVisaType
from app.models.eligibility_rule import EligibilityRuleCategory


class EligibilityRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    # Optional — omit or null → ``other`` (DB column is NOT NULL).
    category: EligibilityRuleCategory | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    points: float = Field(ge=0, le=100)
    weightage_pct: float = Field(ge=0, le=100)
    is_active: bool = True

    @field_validator("category", mode="before")
    @classmethod
    def _category_null_ok(cls, value: object) -> object:
        return EligibilityRuleCategory.other if value is None else value

    @model_validator(mode="after")
    def _default_category(self) -> EligibilityRuleCreate:
        if self.category is None:
            self.category = EligibilityRuleCategory.other
        return self


class EligibilityRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    category: EligibilityRuleCategory | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    points: float | None = Field(default=None, ge=0, le=100)
    weightage_pct: float | None = Field(default=None, ge=0, le=100)
    is_active: bool | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _category_null_means_other(cls, value: object) -> object:
        # Explicit null on PATCH clears to the default bucket, not SQL NULL.
        return EligibilityRuleCategory.other if value is None else value


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
