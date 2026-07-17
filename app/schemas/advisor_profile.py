"""Schemas for advisor profile (private and public views) and onboarding."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from app.core.visa_types import RequiredVisaType
from app.models.advisor_credential import DocumentType
from app.models.advisor_profile import AdvisorServiceType
from app.models.user import VerificationStatus
from app.models.visa_type import VisaType

CountryCode = Annotated[str, Field(min_length=2, max_length=2)]

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


class ServiceOffering(BaseModel):
    """Bookable offering (duration + price) — managed via profile, not onboarding."""

    service_type: str = Field(min_length=1, max_length=100)
    duration_minutes: int = Field(ge=15, le=480)
    price_usd: float = Field(ge=0)


class AdvisorProfileUpdate(BaseModel):
    """Editable advisor profile fields.

    ``successful_applications`` is GET-only (not accepted here). Upload headshots
    via ``POST /uploads`` (``category=profile_photo``), then set ``profile_photo_url``.
    """

    title: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=800)
    profile_photo_url: str | None = None
    country_of_residence: CountryCode | None = None
    expertise_description: str | None = Field(default=None, max_length=2000)
    years_of_experience: int | None = Field(default=None, ge=0, le=60)
    successful_application_rate: float | None = Field(default=None, ge=0, le=100)
    visa_specializations: list[RequiredVisaType] | None = None
    country_expertise: list[CountryCode] | None = None
    offered_services: list[AdvisorServiceType] | None = None
    languages: list[LanguageEntry] | None = None
    services: list[ServiceOffering] | None = None
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
    country_of_residence: str | None
    expertise_description: str | None
    years_of_experience: int | None
    successful_applications: int | None
    successful_application_rate: float | None
    offered_services: list[str]
    visa_specializations: list[VisaType]
    country_expertise: list[str]
    languages: list[LanguageEntry]
    services: list[ServiceOffering]
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

    Matches the Global Jump advisor onboarding screens:

      Step 1 – What services do you offer?          → service_types
      Step 2 – What are your areas of expertise?    → areas_of_expertise
      Step 3 – Specify your service                 → expertise_description
      Step 4 – Where are you based / operate?       → country_of_residence,
                                                       countries_you_serve
      Step 5 – Tell us about yourself               → bio, years_of_experience
      Step 6 – Verification Documents               → documents
                                                      (government_id, license,
                                                       certification)
      Step 7 – Approval Pending                     → sets verification_status
                                                      to under_review (UI only)
    """

    # Step 1
    service_types: list[AdvisorServiceType] = Field(default_factory=list, max_length=20)
    # Step 2
    areas_of_expertise: list[RequiredVisaType] = Field(default_factory=list, max_length=20)
    # Step 3
    expertise_description: str | None = Field(default=None, max_length=2000)
    # Step 4
    country_of_residence: CountryCode | None = None
    countries_you_serve: list[CountryCode] = Field(default_factory=list, max_length=50)
    # Step 5
    bio: str | None = Field(default=None, max_length=800)
    years_of_experience: int | None = Field(default=None, ge=0, le=60)
    # Step 6
    documents: list[OnboardingDocumentRef] = Field(default_factory=list, max_length=20)


class AdvisorOnboardingStatusRead(BaseModel):
    """Status checklist for the post-submit "Approval Pending" / Status Tracking screen."""

    verification_status: VerificationStatus | None
    area_of_expertise_completed: bool
    profile_completed: bool
    government_id_uploaded: bool
    license_uploaded: bool
    certification_uploaded: bool


class AdvisorVerificationResubmitRead(BaseModel):
    """Response after a rejected advisor resubmits their account for review."""

    verification_status: VerificationStatus
    message: str = "Application resubmitted and is under review"


class AdvisorOnboardingCompleteRead(AdvisorProfileRead):
    """Onboarding submit response — profile plus approval-pending checklist."""

    onboarding_status: AdvisorOnboardingStatusRead


class AdvisorProfilePublicRead(BaseModel):
    user_id: uuid.UUID
    full_name: str | None
    title: str | None
    bio: str | None
    profile_photo_url: str | None
    country_of_residence: str | None
    expertise_description: str | None
    years_of_experience: int | None
    successful_applications: int | None
    successful_application_rate: float | None
    offered_services: list[str]
    visa_specializations: list[VisaType]
    country_expertise: list[str]
    languages: list[LanguageEntry]
    services: list[ServiceOffering]
    is_featured: bool
    public_profile_slug: str | None
    match_percentage: int | None = None  # 0–100 for seeker; null without destination/visa context
    is_bookmarked: bool = False  # true when the current seeker has bookmarked this advisor
