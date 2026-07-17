"""Schemas for advisor matching weight sliders (AI Engine Management)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, model_validator


class MatchingWeightsRead(BaseModel):
    id: uuid.UUID | None = None
    country_weight: float
    language_weight: float
    availability_weight: float
    setting_weight: float


class MatchingWeightsUpdate(BaseModel):
    country_weight: float = Field(ge=0, le=100)
    language_weight: float = Field(ge=0, le=100)
    availability_weight: float = Field(ge=0, le=100)
    setting_weight: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _sum_near_100(self) -> MatchingWeightsUpdate:
        total = (
            self.country_weight
            + self.language_weight
            + self.availability_weight
            + self.setting_weight
        )
        if abs(total - 100.0) > 0.5:
            raise ValueError("Matching weights must sum to 100")
        return self
