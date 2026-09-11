"""Schemas for advisor profile (private and public views) and onboarding."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

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

from app.core.countries import SUPPORTED_COUNTRY_CODES, is_supported_country
from app.core.visa_types import RequiredVisaType
from app.models.advisor_credential import DocumentType
from app.models.user import VerificationStatus
from app.models.visa_type import VisaType
from app.schemas.assessment import AiMatchStatusRead
from app.schemas.availability import WeeklySlotInput, WeeklySlotRead

CountryCode = Annotated[str, Field(min_length=2, max_length=2)]
ServiceId = uuid.UUID
LanguageName = Annotated[str, Field(min_length=1, max_length=100)]


def _valid_iana_timezone(v: str) -> str:
    try:
        ZoneInfo(v)
    except Exception as exc:  # noqa: BLE001 — zoneinfo raises several types
        raise ValueError(f"Unknown IANA timezone: {v}") from exc
    return v


# IANA timezone name (e.g. "Asia/Karachi"), validated against the tz database.
IanaTimezone = Annotated[str, Field(max_length=64), AfterValidator(_valid_iana_timezone)]


def _require_supported_destination(value: str) -> str:
    code = value.upper()
    if not is_supported_country(code):
        raise ValueError(
            f"Unsupported country {value!r}; must be one of {', '.join(SUPPORTED_COUNTRY_CODES)}"
        )
    return code


SupportedCountryCode = Annotated[
    str, Field(min_length=2, max_length=2), AfterValidator(_require_supported_destination)
]

# Document types shown on the onboarding "Verification Documents" screen.
ONBOARDING_DOCUMENT_TYPES = frozenset(
    {
        DocumentType.government_id,
        DocumentType.license,
        DocumentType.certification,
    }
)


class LanguageEntry(BaseModel):
    language: str = Field(min_length=1, max_length=100)
    proficiency: Literal["basic", "conversational", "fluent", "native"]


# ── Offered services (ID identity + display name) ────────────────────────────


class OfferedServicePublicRead(BaseModel):
    """Seeker / public view — includes price and duration.

    Catalog rows reuse this shape but always report ``price_usd`` null.
    """

    id: uuid.UUID
    service_id: uuid.UUID
    name: str
    price_usd: float | None = None
    duration_minutes: int = 30


class OfferedServiceRead(BaseModel):
    """Advisor / admin view — includes optional price and duration."""

    id: uuid.UUID
    service_id: uuid.UUID
    name: str
    price_usd: float | None = None
    duration_minutes: int = 30
    has_active_bookings: bool = False


class AdminOfferedServiceRead(BaseModel):
    """Admin catalog list row — identity only (no price/duration)."""

    id: uuid.UUID
    service_id: uuid.UUID
    name: str


class OfferedServiceItemInput(BaseModel):
    """One catalog service row selected by an advisor."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    service_id: uuid.UUID = Field(
        validation_alias=AliasChoices("service_id", "serviceId"),
    )
    price_usd: float = Field(
        gt=0,
        validation_alias=AliasChoices("price_usd", "priceUsd", "price"),
    )
    duration_minutes: int = Field(
        default=30,
        ge=15,
        le=480,
        validation_alias=AliasChoices("duration_minutes", "durationMinutes", "duration"),
    )

    @field_validator("price_usd", mode="before")
    @classmethod
    def _blank_price(cls, value: object) -> object:
        if value is None or value == "":
            raise ValueError("A positive price is required for every selected service")
        return value

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _blank_duration_default(cls, value: object) -> object:
        if value is None or value == "":
            return 30
        return value


class OfferedServiceCreateRequest(BaseModel):
    """Create a catalog service or attach a catalog service to an advisor."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    service_id: uuid.UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("service_id", "serviceId"),
    )
    name: str | None = Field(default=None, min_length=1, max_length=100)
    price_usd: float | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("price_usd", "priceUsd", "price"),
    )
    duration_minutes: int = Field(
        default=30,
        ge=15,
        le=480,
        validation_alias=AliasChoices("duration_minutes", "durationMinutes", "duration"),
    )

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("price_usd", mode="before")
    @classmethod
    def _blank_price(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _blank_duration_default(cls, value: object) -> object:
        if value is None or value == "":
            return 30
        return value


class AdminOfferedServiceCreateRequest(OfferedServiceCreateRequest):
    """Admin create — ``advisor_id`` optional (omit to create a global catalog row)."""

    advisor_id: uuid.UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("advisor_id", "advisorId"),
    )

    @field_validator("advisor_id", mode="before")
    @classmethod
    def _blank_advisor_id(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value


class OfferedServiceUpdateRequest(BaseModel):
    """Partial update — send only the fields you want to change.

    Empty strings are treated as omitted (``None``) so admin forms that submit
    blank inputs for untouched fields do not 422.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    price_usd: float | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("price_usd", "priceUsd", "price"),
    )
    duration_minutes: int | None = Field(
        default=None,
        ge=15,
        le=480,
        validation_alias=AliasChoices("duration_minutes", "durationMinutes", "duration"),
    )

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("price_usd", mode="before")
    @classmethod
    def _blank_price(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _blank_duration(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value


class OfferedServicesReplaceRequest(BaseModel):
    """Replace an advisor's full offered-services set (advisor self-service / onboarding)."""

    services: list[OfferedServiceItemInput] = Field(default_factory=list, max_length=20)


class AdvisorProfileUpdate(BaseModel):
    """Editable advisor profile fields.

    ``successful_applications`` is GET-only (not accepted here). Upload headshots
    via ``POST /uploads`` (``category=profile_photo``), then set ``profile_photo_url``.
    Upload banners via ``category=profile_banner``, then set ``banner_url``.
    """

    title: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=800)
    profile_photo_url: str | None = None
    banner_url: str | None = None
    country_of_residence: CountryCode | None = None
    timezone: IanaTimezone | None = None
    education: str | None = Field(default=None, max_length=200)
    expertise_description: str | None = Field(default=None, max_length=2000)
    years_of_experience: int | None = Field(default=None, ge=0, le=60)
    successful_application_rate: float | None = Field(default=None, ge=0, le=100)
    visa_specializations: list[RequiredVisaType] | None = None
    country_expertise: list[SupportedCountryCode] | None = None
    offered_services: list[ServiceId] | None = None
    languages: list[LanguageEntry] | None = None
    weekly_slots: list[WeeklySlotInput] | None = Field(
        default=None,
        description="Weekly working hours; sending replaces saved hours, [] clears them.",
    )
    public_profile_slug: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )


class AdvisorProfileRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    bio: str | None
    profile_photo_url: str | None
    banner_url: str | None
    country_of_residence: str | None
    timezone: str | None
    education: str | None
    expertise_description: str | None
    years_of_experience: int | None
    successful_applications: int | None
    successful_application_rate: float | None
    visa_specializations: list[VisaType]
    country_expertise: list[str]
    languages: list[LanguageEntry]
    weekly_slots: list[WeeklySlotRead] = []
    starting_price_usd: float | None = None
    is_featured: bool
    public_profile_slug: str | None
    # Read-only / derived
    average_rating: float | None = None
    review_count: int = 0
    avg_response_time_hours: float | None = None
    verification_status: VerificationStatus | None = None
    match_percentage: int | None = None  # seeker-context only; null on /me/profile
    created_at: datetime
    updated_at: datetime


class AdvisorListingCard(BaseModel):
    """Compact card for the advisor list/search endpoint (PRD §3.5)."""

    user_id: uuid.UUID
    full_name: str | None
    email: str
    title: str | None
    profile_photo_url: str | None
    years_of_experience: int | None
    offered_services: list[str]
    visa_specializations: list[VisaType]
    country_expertise: list[str]
    languages: list[str]
    starting_price_usd: float | None
    average_rating: float | None
    review_count: int
    is_featured: bool
    public_profile_slug: str | None
    match_percentage: int | None = None  # 0–100 for seeker; null without destination/visa context
    is_bookmarked: bool = False  # true when the current seeker has bookmarked this advisor
    # Existing chat thread with the current seeker; null if none / caller is not a seeker
    conversation_id: uuid.UUID | None = None


class DocumentUploadResult(BaseModel):
    """Returned immediately after a single document file is uploaded during onboarding.

    The frontend holds ``file_key`` in browser storage and includes it in the
    final ``AdvisorOnboardingSubmit`` payload so the server can link the
    already-stored file to the new credential record.
    """

    file_key: str
    file_url: str
    document_type: DocumentType


class OnboardingDocumentRef(BaseModel):
    """Reference to a file previously uploaded via ``POST /uploads``."""

    file_key: str = Field(min_length=1, max_length=500)
    document_type: DocumentType
    document_name: str = Field(min_length=1, max_length=255)
    expiry_date: date | None = None

    @field_validator("document_type")
    @classmethod
    def _onboarding_document_type(cls, value: DocumentType) -> DocumentType:
        # Accept legacy ``immigration_license`` as the design's "License Upload".
        if value == DocumentType.immigration_license:
            return DocumentType.license
        if value not in ONBOARDING_DOCUMENT_TYPES:
            raise ValueError(
                "Onboarding documents must be one of: government_id, license, certification"
            )
        return value


class AdvisorOnboardingSubmit(BaseModel):
    """Single-shot payload POSTed at the final advisor onboarding wizard step.

    Matching-relevant fields (mirror seeker onboarding signals):

      service_ids / services  → advisor ``offered_services`` (vs seeker needed services)
      languages                 → advisor ``languages`` (vs seeker preferred_languages)
      areas_of_expertise        → visa specializations (vs seeker intended_visa_type)
      countries_you_serve       → country expertise (vs seeker intended_destination)

    Wizard screens:

      Step 1 – What services do you offer?          → service_ids (+ optional priced
                                                       ``services`` / weekly_slots)
      Step 2 – What are your areas of expertise?    → areas_of_expertise
      Step 3 – Specify your service                 → expertise_description
      Step 4 – Where are you based / operate?       → country_of_residence,
                                                       countries_you_serve
      Step 5 – Tell us about yourself               → bio, years_of_experience,
                                                       languages
      Step 6 – Verification Documents               → documents
      Step 7 – Approval Pending                     → under_review (UI only)
    """

    # Step 1 — catalog name slugs or catalog UUIDs (same as seeker ``services``).
    service_ids: list[ServiceId] = Field(default_factory=list, max_length=20)
    # Step 1 (optional) — priced offered-service rows (merged into offered_services).
    services: list[OfferedServiceItemInput] | None = Field(default=None, max_length=20)
    # Step 1 (optional) — weekly working hours; [] or omit means no hours set.
    weekly_slots: list[WeeklySlotInput] | None = Field(default=None, max_length=100)
    # Step 2
    areas_of_expertise: list[RequiredVisaType] = Field(default_factory=list, max_length=20)
    # Step 3
    expertise_description: str | None = Field(default=None, max_length=2000)
    # Step 4
    country_of_residence: CountryCode | None = None
    countries_you_serve: list[SupportedCountryCode] = Field(default_factory=list, max_length=50)
    # Step 5
    bio: str | None = Field(default=None, max_length=800)
    years_of_experience: int | None = Field(default=None, ge=0, le=60)
    # Step 5 — languages spoken (matching soft signal). ``None`` = leave unchanged.
    languages: list[LanguageEntry] | None = Field(default=None, max_length=20)
    # Convenience: bare language name strings (same idea as seeker preferred_languages).
    language_names: list[LanguageName] | None = Field(default=None, max_length=20)
    # Step 6
    documents: list[OnboardingDocumentRef] = Field(default_factory=list, max_length=20)

    @field_validator("language_names", mode="before")
    @classmethod
    def _coerce_string_list(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        return value

    @field_validator("languages", mode="before")
    @classmethod
    def _coerce_languages(cls, value: object) -> object:
        """Accept ``LanguageEntry`` objects or bare language name strings."""
        if value is None:
            return None
        if not isinstance(value, list):
            return value
        out: list[object] = []
        for item in value:
            if isinstance(item, str):
                lang = item.strip()
                if lang:
                    out.append({"language": lang, "proficiency": "fluent"})
            else:
                out.append(item)
        return out

    @model_validator(mode="after")
    def _merge_language_names(self) -> AdvisorOnboardingSubmit:
        if self.languages is None and self.language_names:
            self.languages = [
                LanguageEntry(language=name, proficiency="fluent")
                for name in self.language_names
                if name and name.strip()
            ]
        return self


class AdvisorOnboardingStatusRead(BaseModel):
    """Status checklist for the post-submit "Approval Pending" / Status Tracking screen."""

    verification_status: VerificationStatus | None
    area_of_expertise_completed: bool
    profile_completed: bool
    languages_completed: bool = False
    services_completed: bool = False
    government_id_uploaded: bool
    license_uploaded: bool
    certification_uploaded: bool


class AdvisorVerificationResubmitRead(BaseModel):
    """Response after a rejected advisor resubmits their account for review."""

    verification_status: VerificationStatus
    message: str = "Application resubmitted and is under review"


class SeekerMatchRead(BaseModel):
    """Seeker card for advisor-facing AI recommendations (inverse of AdvisorMatchRead)."""

    user_id: uuid.UUID
    full_name: str | None
    email: str | None = None
    profile_photo_url: str | None = None
    intended_destination: str | None = None
    intended_visa_type: str | None = None
    preferred_languages: list[str] = Field(default_factory=list)
    service_ids: list[uuid.UUID] = Field(default_factory=list)
    match_score: float
    match_reasons: str | None = None
    rule_score: float | None = None
    ai_score: float | None = None


class AdvisorOnboardingCompleteRead(AdvisorProfileRead):
    """Onboarding submit response — profile, checklist, and AI-matched seekers."""

    onboarding_status: AdvisorOnboardingStatusRead
    matched_seekers: list[SeekerMatchRead] = Field(default_factory=list)
    ai_match: AiMatchStatusRead | None = None


class AdvisorProfilePublicRead(BaseModel):
    user_id: uuid.UUID
    full_name: str | None
    email: EmailStr | None
    title: str | None
    bio: str | None
    profile_photo_url: str | None
    banner_url: str | None
    country_of_residence: str | None
    timezone: str | None
    education: str | None
    expertise_description: str | None
    years_of_experience: int | None
    successful_applications: int | None
    successful_application_rate: float | None
    visa_specializations: list[VisaType]
    country_expertise: list[str]
    languages: list[LanguageEntry]
    offered_services: list[OfferedServicePublicRead] = Field(default_factory=list)
    starting_price_usd: float | None = None
    is_featured: bool
    public_profile_slug: str | None
    match_percentage: int | None = None  # 0–100 for seeker; null without destination/visa context
    is_bookmarked: bool = False  # true when the current seeker has bookmarked this advisor
