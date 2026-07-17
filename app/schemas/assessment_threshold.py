"""Schemas for admin-configurable assessment score thresholds (PRD §3.4)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, model_validator

from app.core.visa_types import OptionalVisaType


class AssessmentThresholdUpsert(BaseModel):
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    highly_eligible_min: float = Field(ge=0, le=100)
    likely_eligible_min: float = Field(ge=0, le=100)
    borderline_min: float = Field(ge=0, le=100)
    is_active: bool = True

    @model_validator(mode="after")
    def _descending_order(self) -> AssessmentThresholdUpsert:
        if not (self.highly_eligible_min > self.likely_eligible_min > self.borderline_min):
            raise ValueError(
                "highly_eligible_min must be greater than likely_eligible_min, "
                "which must be greater than borderline_min"
            )
        return self


class AssessmentThresholdRead(BaseModel):
    id: uuid.UUID
    country_code: str | None
    visa_type: OptionalVisaType
    highly_eligible_min: float
    likely_eligible_min: float
    borderline_min: float
    is_active: bool
