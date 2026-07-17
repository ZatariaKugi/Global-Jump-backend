"""Schemas for seeker profile read / update."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from app.core.countries import country_code
from app.core.visa_types import OptionalVisaType, RequiredVisaType
from app.models.seeker_profile import EducationLevel, EmploymentStatus

CountryCode = Annotated[str, Field(min_length=2, max_length=2)]


class PriorVisa(BaseModel):
    country: CountryCode
    visa_type: RequiredVisaType
    year: int = Field(ge=1900, le=2100)


class SeekerProfileUpdate(BaseModel):
    date_of_birth: date | None = None
    nationality: CountryCode | None = None
    country_of_residence: CountryCode | None = None
    profile_photo_url: str | None = None
    intended_visa_type: OptionalVisaType = None
    intended_destination: CountryCode | None = None
    passport_number: str | None = Field(default=None, min_length=5, max_length=20)
    passport_expiry: date | None = None
    countries_visited: list[CountryCode] | None = None
    prior_visas: list[PriorVisa] | None = None
    education_level: EducationLevel | None = None
    employment_status: EmploymentStatus | None = None
    employer_name: str | None = Field(default=None, max_length=255)
    annual_income_band: str | None = Field(default=None, max_length=50)
    has_bank_statements: bool | None = None
    email_notifications: bool | None = None


class OnboardingSubmit(BaseModel):
    """Single-shot payload POSTed by the frontend at the final onboarding wizard step.

    The frontend collects data across all wizard steps in browser storage and
    POSTs this once at the end.  Fields map to wizard screens:

      Step 1 – visa intent         → intended_visa_type
      Step 2 – destination country → intended_destination (accepts a full country name or a
                                      2-letter code; resolved to the ISO code server-side)
      Step 3 – finance             → annual_income_band
      Step 4 – travel history      → countries_visited (self-reported band, e.g. "1-2
                                      countries" — not a list of actual countries)
      Step 5 – AI assessment       → matching_opportunities (categories selected on the
                                      "Matching you with suitable opportunities" screen);
                                      AI suggestions are generated from steps 1-5 and
                                      returned in the response
      Steps 5/6 (optional, user may skip) → employment_status, education_level, nationality
                                      (nationality accepts a full country name or a 2-letter
                                      code, same as intended_destination)
    """

    # Step 1
    intended_visa_type: RequiredVisaType
    # Step 2 — full country name (e.g. "Japan") or 2-letter code; normalised to the code below
    intended_destination: str = Field(min_length=2, max_length=100)
    # Step 3
    annual_income_band: str = Field(min_length=1, max_length=50)
    # Step 4 — self-reported travel-history band, free text (e.g. "Traveled to 1-2 countries",
    # "Never traveled outside my home country"); not linked to the profile's actual
    # list of visited countries, which is set separately via PATCH /users/me/profile
    countries_visited: str = Field(default="", max_length=100)
    # Step 5 — opportunity categories selected on the AI assessment "matching" screen,
    # e.g. ["visa_type", "interest", "finance", "travel_history", "documentation"]
    matching_opportunities: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=20
    )
    # Steps 5-6 (optional — user may skip) — full country name (e.g. "Pakistan") or 2-letter
    # code; normalised to the code below
    nationality: str | None = Field(default=None, min_length=2, max_length=100)
    education_level: EducationLevel | None = None
    employment_status: EmploymentStatus | None = None
    employer_name: str | None = Field(default=None, max_length=255)

    @field_validator("intended_destination")
    @classmethod
    def _resolve_intended_destination(cls, value: str) -> str:
        code = country_code(value)
        if code is None:
            raise ValueError(f"Unrecognized country: {value!r}")
        return code

    @field_validator("nationality")
    @classmethod
    def _resolve_nationality(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = country_code(value)
        if code is None:
            raise ValueError(f"Unrecognized country: {value!r}")
        return code


class SeekerProfileRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    date_of_birth: date | None
    nationality: str | None
    country_of_residence: str | None
    profile_photo_url: str | None
    intended_visa_type: OptionalVisaType
    intended_destination: str | None
    passport_number_masked: str | None = None
    passport_expiry: date | None
    countries_visited: list[str]
    prior_visas: list[PriorVisa]
    education_level: EducationLevel | None
    employment_status: EmploymentStatus | None
    employer_name: str | None
    annual_income_band: str | None
    has_bank_statements: bool
    email_notifications: bool
    created_at: datetime
    updated_at: datetime


class OnboardingCompleteRead(SeekerProfileRead):
    """Onboarding response — the profile plus Step 5 AI-generated suggestions."""

    ai_suggestions: list[str] = Field(default_factory=list)
