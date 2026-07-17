"""Schemas for assessment A/B test variants (AI Engine Management)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.core.visa_types import OptionalVisaType


class AbVariantCreate(BaseModel):
    label: str = Field(min_length=1, max_length=8)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    is_active: bool = True


class AbVariantUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=8)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    is_active: bool | None = None


class AbVariantRead(BaseModel):
    id: uuid.UUID
    label: str
    name: str
    description: str | None
    country_code: str | None
    visa_type: OptionalVisaType
    is_active: bool
    started_count: int = 0
    completed_count: int = 0
    conversion_rate: float = 0.0
