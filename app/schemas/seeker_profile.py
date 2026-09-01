"""Schemas for seeker profile read / update."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.core.countries import SUPPORTED_COUNTRY_CODES, country_code, is_supported_country
from app.core.visa_types import OptionalVisaType, RequiredVisaType, parse_visa_type
from app.models.seeker_profile import EducationLevel, EmploymentStatus
from app.schemas.assessment import AdvisorMatchRead, AiMatchStatusRead

CountryCode = Annotated[str, Field(min_length=2, max_length=2)]
LanguageName = Annotated[str, Field(min_length=1, max_length=100)]
ServiceTypeSlug = Annotated[str, Field(min_length=1, max_length=100)]


def _expand_comma_separated_list(value: object) -> object:
    """Accept a string, or a list that may contain comma-joined items.

    FE sometimes sends ``["English, Spanish"]`` or ``"English, Spanish"`` —
    normalize to ``["English", "Spanish"]``.
    """
    if value is None:
        return value
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        out: list[object] = []
        seen: set[str] = set()
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                parts = [p.strip() for p in item.split(",") if p.strip()]
            else:
                parts = [item]
            for part in parts:
                key = str(part).strip().casefold()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(part.strip() if isinstance(part, str) else part)
        return out
    return value



def _require_supported_destination(value: str) -> str:
    code = value.upper()
    if not is_supported_country(code):
        raise ValueError(
            f"Unsupported destination {value!r}; must be one of "
            f"{', '.join(SUPPORTED_COUNTRY_CODES)}"
        )
    return code


def _resolve_destination_code(value: str) -> str:
    code = country_code(value)
    if code is None:
        raise ValueError(f"Unrecognized country: {value!r}")
    if not is_supported_country(code):
        raise ValueError(
            f"Unsupported destination {value!r}; must be one of "
            f"{', '.join(SUPPORTED_COUNTRY_CODES)}"
        )
    return code


def _resolve_visa_slug(value: str) -> str:
    parsed = parse_visa_type(value)
    if parsed is None:
        raise ValueError(f"Unrecognized visa type: {value!r}")
    return parsed.value


SupportedDestinationCode = Annotated[
    str, Field(min_length=2, max_length=2), AfterValidator(_require_supported_destination)
]


class PriorVisa(BaseModel):
    country: CountryCode
    visa_type: RequiredVisaType
    year: int = Field(ge=1900, le=2100)


class SeekerProfileUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    date_of_birth: date | None = None
    nationality: CountryCode | None = None
    country_of_residence: CountryCode | None = None
    profile_photo_url: str | None = None
    banner_url: str | None = None
    phone: str | None = Field(default=None, max_length=40)
    timezone: str | None = Field(default=None, max_length=50)
    preferred_languages: list[LanguageName] | None = None
    # Legacy singular field still sent by the current profile editor.
    preferred_language: str | None = Field(default=None, max_length=100)
    about: str | None = Field(default=None, max_length=2000)
    # Multi-value intent (preferred). Singular fields kept as legacy aliases.
    intended_visa_types: list[RequiredVisaType] | None = None
    intended_visa_type: OptionalVisaType = None
    intended_destinations: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "intended_destinations",
            "intendedDestinations",
            "destination_country_ids",
            "destinationCountryIds",
            "countries",  # same table as GET intended_destinations
        ),
    )
    intended_destination: SupportedDestinationCode | None = None
    passport_number: str | None = Field(default=None, min_length=5, max_length=20)
    passport_expiry: date | None = None
    countries_visited: list[CountryCode] | None = None
    prior_visas: list[PriorVisa] | None = None
    needed_services: list[ServiceTypeSlug] | None = None
    # Alias accepted from onboarding FE drafts.
    service_ids: list[ServiceTypeSlug] | None = None
    education_level: EducationLevel | None = None
    employment_status: EmploymentStatus | None = None
    employer_name: str | None = Field(default=None, max_length=255)
    annual_income_band: str | None = Field(default=None, max_length=50)
    has_bank_statements: bool | None = None
    email_notifications: bool | None = None

    @model_validator(mode="after")
    def _normalize_multi_fields(self) -> SeekerProfileUpdate:
        if self.preferred_languages is None and self.preferred_language is not None:
            parts = [p.strip() for p in self.preferred_language.split(",")]
            self.preferred_languages = [p for p in parts if p]
        elif self.preferred_languages is not None:
            # Expand any remaining comma-joined entries inside the list.
            expanded = _expand_comma_separated_list(self.preferred_languages)
            self.preferred_languages = (
                list(expanded) if isinstance(expanded, list) else self.preferred_languages
            )

        if self.intended_visa_types is None and self.intended_visa_type is not None:
            self.intended_visa_types = [self.intended_visa_type]
        if self.intended_destinations is None and self.intended_destination is not None:
            self.intended_destinations = [self.intended_destination]

        # Prefer service_ids when needed_services omitted or empty (FE onboarding draft).
        if not self.needed_services and self.service_ids:
            self.needed_services = list(self.service_ids)

        # Keep singular mirrors in sync with the first multi value when arrays are set.
        if self.intended_visa_types is not None:
            self.intended_visa_type = (
                self.intended_visa_types[0] if self.intended_visa_types else None
            )
        if self.intended_destinations is not None:
            self.intended_destination = (
                self.intended_destinations[0] if self.intended_destinations else None
            )
        return self

    @field_validator("intended_destinations", mode="before")
    @classmethod
    def _coerce_destinations(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        return value

    @field_validator("intended_destinations")
    @classmethod
    def _resolve_destinations(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        resolved: list[str] = []
        seen: set[str] = set()
        for item in value:
            code = _resolve_destination_code(str(item))
            if code in seen:
                continue
            seen.add(code)
            resolved.append(code)
        return resolved

    @field_validator("intended_visa_types", mode="before")
    @classmethod
    def _coerce_visa_types(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        return value

    @field_validator("preferred_languages", "needed_services", "service_ids", mode="before")
    @classmethod
    def _coerce_string_lists(cls, value: object) -> object:
        return _expand_comma_separated_list(value)


class OnboardingSubmit(BaseModel):
    """Single-shot payload POSTed by the frontend at the final onboarding wizard step.

    Multi-value fields (preferred):

      intended_visa_types / intended_visa_type
      intended_destinations / intended_destination
      preferred_languages / preferred_language
      services / service_ids / needed_services

    Singular fields remain accepted for backward compatibility and are expanded
    into one-item lists when the plural form is omitted.
    """

    intended_visa_types: list[RequiredVisaType] = Field(default_factory=list, max_length=20)
    intended_visa_type: RequiredVisaType | None = None
    intended_destinations: list[str] = Field(default_factory=list, max_length=20)
    intended_destination: str | None = Field(default=None, min_length=2, max_length=100)
    annual_income_band: str = Field(min_length=1, max_length=50)
    countries_visited: str = Field(default="", max_length=100)
    preferred_languages: list[LanguageName] = Field(default_factory=list, max_length=20)
    preferred_language: str | None = Field(default=None, max_length=100)
    services: list[ServiceTypeSlug] = Field(default_factory=list, max_length=20)
    service_ids: list[ServiceTypeSlug] = Field(default_factory=list, max_length=20)
    nationality: str | None = Field(default=None, min_length=2, max_length=100)
    country_of_residence: str | None = Field(default=None, min_length=2, max_length=100)
    education_level: EducationLevel | None = None
    employment_status: EmploymentStatus | None = None
    employer_name: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _normalize_onboarding_lists(self) -> OnboardingSubmit:
        if not self.preferred_languages and self.preferred_language:
            # Support both a single value and a legacy comma-joined string.
            parts = [p.strip() for p in self.preferred_language.split(",")]
            self.preferred_languages = [p for p in parts if p]
        elif self.preferred_languages:
            expanded = _expand_comma_separated_list(self.preferred_languages)
            self.preferred_languages = (
                list(expanded) if isinstance(expanded, list) else self.preferred_languages
            )

        if not self.intended_visa_types and self.intended_visa_type is not None:
            self.intended_visa_types = [self.intended_visa_type]
        if not self.intended_destinations and self.intended_destination:
            self.intended_destinations = [self.intended_destination]
        # FE sends service_ids; keep services in sync even when services=[] was defaulted.
        if not self.services and self.service_ids:
            self.services = list(self.service_ids)
        elif not self.service_ids and self.services:
            self.service_ids = list(self.services)

        if not self.intended_visa_types:
            raise ValueError("At least one intended visa type is required")
        if not self.intended_destinations:
            raise ValueError("At least one intended destination is required")

        # Primary mirrors for legacy columns / matching primary case.
        self.intended_visa_type = self.intended_visa_types[0]
        self.intended_destination = self.intended_destinations[0]

        return self

    @field_validator("intended_destinations", mode="before")
    @classmethod
    def _coerce_destinations(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        return value

    @field_validator("intended_destinations")
    @classmethod
    def _resolve_destinations(cls, value: list[str]) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()
        for item in value:
            code = _resolve_destination_code(item)
            if code in seen:
                continue
            seen.add(code)
            resolved.append(code)
        return resolved

    @field_validator("intended_destination")
    @classmethod
    def _resolve_intended_destination(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _resolve_destination_code(value)

    @field_validator("intended_visa_types", mode="before")
    @classmethod
    def _coerce_visa_types(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        return value

    @field_validator("intended_visa_types")
    @classmethod
    def _resolve_visa_types(cls, value: list[object]) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()
        for item in value:
            slug = _resolve_visa_slug(str(item))
            if slug in seen:
                continue
            seen.add(slug)
            resolved.append(slug)
        return resolved

    @field_validator("nationality")
    @classmethod
    def _resolve_nationality(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = country_code(value)
        if code is None:
            raise ValueError(f"Unrecognized country: {value!r}")
        return code

    @field_validator("country_of_residence")
    @classmethod
    def _resolve_country_of_residence(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = country_code(value)
        if code is None:
            raise ValueError(f"Unrecognized country: {value!r}")
        return code

    @field_validator(
        "preferred_languages",
        "services",
        "service_ids",
        mode="before",
    )
    @classmethod
    def _coerce_string_list(cls, value: object) -> object:
        """Accept a bare string, comma-joined string, or list with joined items."""
        return _expand_comma_separated_list(value)


class SeekerProfileRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr | None = None
    date_of_birth: date | None
    nationality: str | None
    profile_photo_url: str | None
    banner_url: str | None
    phone: str | None
    timezone: str | None
    preferred_languages: list[str] = Field(default_factory=list)
    about: str | None
    intended_visa_types: list[str] = Field(default_factory=list)
    intended_destinations: list[str] = Field(default_factory=list)
    passport_number_masked: str | None = None
    passport_expiry: date | None
    countries_visited: list[str]
    prior_visas: list[PriorVisa]
    service_ids: list[str] = Field(default_factory=list)
    education_level: EducationLevel | None
    employment_status: EmploymentStatus | None
    employer_name: str | None
    annual_income_band: str | None
    has_bank_statements: bool
    email_notifications: bool
    created_at: datetime
    updated_at: datetime
    ai_match: AiMatchStatusRead | None = None


class OnboardingCompleteRead(SeekerProfileRead):
    """Onboarding response — saved profile plus matched advisors."""

    matched_advisors: list[AdvisorMatchRead] = Field(default_factory=list)
