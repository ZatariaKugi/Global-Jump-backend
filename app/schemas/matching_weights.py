"""Schemas for advisor matching weight sliders (AI Engine Management)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, model_validator


class MatchingWeightsRead(BaseModel):
    id: uuid.UUID | None = None
    country_weight: float
    visa_weight: float
    language_weight: float
    services_weight: float
    experience_weight: float
    rating_weight: float
    # Legacy aliases for the current admin UI (do not remove until FE migrates).
    availability_weight: float | None = None
    setting_weight: float | None = None


class MatchingWeightsUpdate(BaseModel):
    country_weight: float = Field(ge=0, le=100)
    language_weight: float = Field(ge=0, le=100)
    visa_weight: float | None = Field(default=None, ge=0, le=100)
    services_weight: float | None = Field(default=None, ge=0, le=100)
    experience_weight: float | None = Field(default=None, ge=0, le=100)
    rating_weight: float | None = Field(default=None, ge=0, le=100)
    # Legacy admin UI fields.
    availability_weight: float | None = Field(default=None, ge=0, le=100)
    setting_weight: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _normalize_and_sum(self) -> MatchingWeightsUpdate:
        visa = self.visa_weight if self.visa_weight is not None else self.setting_weight
        services = (
            self.services_weight
            if self.services_weight is not None
            else self.availability_weight
        )
        experience = self.experience_weight if self.experience_weight is not None else 0.0
        rating = self.rating_weight if self.rating_weight is not None else 0.0

        # Legacy 4-slider payload: map setting→visa and availability→services.
        if visa is None or services is None:
            raise ValueError(
                "Provide visa_weight/services_weight "
                "(or legacy setting_weight/availability_weight)"
            )

        self.visa_weight = visa
        self.services_weight = services
        self.experience_weight = experience
        self.rating_weight = rating

        total = (
            self.country_weight
            + self.visa_weight
            + self.language_weight
            + self.services_weight
            + self.experience_weight
            + self.rating_weight
        )
        if abs(total - 100.0) > 0.5:
            raise ValueError("Matching weights must sum to 100")
        return self
