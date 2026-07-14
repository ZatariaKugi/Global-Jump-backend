"""Schemas for advisor profile (private and public views)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.models.advisor_credential import DocumentType

CountryCode = Annotated[str, Field(min_length=2, max_length=2)]


class VisaSpecialization(StrEnum):
    tourist = "tourist"
    work = "work"
    student = "student"
    permanent_residency = "permanent_residency"
    family = "family"
    investment = "investment"
    asylum = "asylum"
    other = "other"


class LanguageEntry(BaseModel):
    language: str = Field(min_length=1, max_length=100)
    proficiency: Literal["basic", "conversational", "fluent", "native"]


class ServiceOffering(BaseModel):
    service_type: str = Field(min_length=1, max_length=100)
    duration_minutes: int = Field(ge=15, le=480)
    price_usd: float = Field(ge=0)


class AdvisorProfileUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=800)
    profile_photo_url: str | None = None
    years_of_experience: int | None = Field(default=None, ge=0, le=60)
    successful_applications: int | None = Field(default=None, ge=0)
    visa_specializations: list[VisaSpecialization] | None = None
    country_expertise: list[CountryCode] | None = None
    languages: list[LanguageEntry] | None = None
    services: list[ServiceOffering] | None = None


class AdvisorProfileRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    bio: str | None
    profile_photo_url: str | None
    years_of_experience: int | None
    successful_applications: int | None
    visa_specializations: list[str]
    country_expertise: list[str]
    languages: list[LanguageEntry]
    services: list[ServiceOffering]
    is_featured: bool
    public_profile_slug: str | None
    created_at: datetime
    updated_at: datetime


class AdvisorListingCard(BaseModel):
    """Compact card for the advisor list/search endpoint (PRD §3.5)."""

    user_id: uuid.UUID
    full_name: str | None
    title: str | None
    profile_photo_url: str | None
    years_of_experience: int | None
    visa_specializations: list[str]
    country_expertise: list[str]
    languages: list[str]
    starting_price_usd: float | None
    average_rating: float | None
    review_count: int
    is_featured: bool
    public_profile_slug: str | None


class DocumentUploadResult(BaseModel):
    """Returned immediately after a single document file is uploaded during onboarding.

    The frontend holds ``file_key`` in browser storage and includes it in the
    final ``AdvisorOnboardingSubmit`` payload so the server can link the
    already-stored file to the new credential record.
    """

    file_key: str  # storage-relative path, e.g. "credentials/{user_id}/immigration_license/abc.pdf"
    file_url: str  # presigned S3 URL or local /uploads path for immediate preview
    document_type: DocumentType


class OnboardingDocumentRef(BaseModel):
    """Reference to a file previously uploaded via the document upload endpoint."""

    file_key: str = Field(min_length=1, max_length=500)
    document_type: DocumentType
    document_name: str = Field(min_length=1, max_length=255)
    expiry_date: date | None = None


class AdvisorOnboardingSubmit(BaseModel):
    """Single-shot payload POSTed by the frontend at the final advisor onboarding wizard step.

    The frontend collects data across all wizard steps in browser storage and
    POSTs this once at the end.  Fields map to wizard screens:

      Step 1  – services offered       → services
      Steps 2-4 – expertise            → visa_specializations, country_expertise
      Step 5  – location               → base_country (prepended to country_expertise)
      Step 6  – professional profile   → title, bio, years_of_experience
      Step 7  – verification documents → documents (keys from upload endpoint)
    """

    # Step 1
    services: list[ServiceOffering] = Field(default_factory=list)
    # Steps 2-4
    visa_specializations: list[VisaSpecialization] = Field(default_factory=list)
    country_expertise: list[CountryCode] = Field(default_factory=list)
    # Step 5 — base country prepended to country_expertise if not already present
    base_country: CountryCode | None = None
    # Step 6
    title: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=800)
    years_of_experience: int | None = Field(default=None, ge=0, le=60)
    successful_applications: int | None = Field(default=None, ge=0)
    # Step 7
    documents: list[OnboardingDocumentRef] = Field(default_factory=list)


class AdvisorProfilePublicRead(BaseModel):
    user_id: uuid.UUID
    full_name: str | None
    title: str | None
    bio: str | None
    profile_photo_url: str | None
    years_of_experience: int | None
    successful_applications: int | None
    visa_specializations: list[str]
    country_expertise: list[str]
    languages: list[LanguageEntry]
    services: list[ServiceOffering]
    is_featured: bool
    public_profile_slug: str | None
