"""Seeker profile — extended personal and travel data for visa eligibility."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel


class EducationLevel(StrEnum):
    high_school = "high_school"
    bachelor = "bachelor"
    master = "master"
    phd = "phd"
    other = "other"


class EmploymentStatus(StrEnum):
    employed = "employed"
    self_employed = "self_employed"
    student = "student"
    unemployed = "unemployed"
    retired = "retired"


class SeekerCountryVisited(Base):
    """One row per country a seeker has visited."""

    __tablename__ = "seeker_countries_visited"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seeker_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)


class SeekerPriorVisa(Base):
    """One row per prior visa a seeker has held."""

    __tablename__ = "seeker_prior_visas"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seeker_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    visa_type: Mapped[str] = mapped_column(String(100), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)


class SeekerPreferredLanguage(Base):
    """One row per language a seeker prefers to communicate in."""

    __tablename__ = "seeker_preferred_languages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seeker_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    language: Mapped[str] = mapped_column(String(100), nullable=False)


class SeekerNeededService(Base):
    __tablename__ = "seeker_needed_services"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seeker_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("advisor_offered_services.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class SeekerIntendedDestination(Base):
    """One row per destination country the seeker selected during onboarding."""

    __tablename__ = "seeker_intended_destinations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seeker_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)


class SeekerIntendedVisaType(Base):
    """One row per visa type the seeker selected during onboarding."""

    __tablename__ = "seeker_intended_visa_types"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seeker_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    visa_type: Mapped[str] = mapped_column(String(50), nullable=False)


class SeekerProfile(BaseModel):
    __tablename__ = "seeker_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Identity
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(2), nullable=True)
    country_of_residence: Mapped[str | None] = mapped_column(String(2), nullable=True)
    profile_photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    banner_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Edit-profile sheet (shared seeker settings)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    about: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Primary onboarding intent (first selected value — kept for matching/legacy reads)
    intended_visa_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    intended_destination: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # Set by POST /users/me/visa-journey/submit once Review is complete.
    application_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Passport — stored AES-256-GCM encrypted; plaintext never persisted
    passport_number_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    passport_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Travel history / preferences — normalised into child tables
    countries_visited: Mapped[list[SeekerCountryVisited]] = relationship(
        "SeekerCountryVisited", cascade="all, delete-orphan", lazy="selectin"
    )
    prior_visas: Mapped[list[SeekerPriorVisa]] = relationship(
        "SeekerPriorVisa", cascade="all, delete-orphan", lazy="selectin"
    )
    preferred_languages: Mapped[list[SeekerPreferredLanguage]] = relationship(
        "SeekerPreferredLanguage", cascade="all, delete-orphan", lazy="selectin"
    )
    needed_services: Mapped[list[SeekerNeededService]] = relationship(
        "SeekerNeededService", cascade="all, delete-orphan", lazy="selectin"
    )
    intended_destinations: Mapped[list[SeekerIntendedDestination]] = relationship(
        "SeekerIntendedDestination", cascade="all, delete-orphan", lazy="selectin"
    )
    intended_visa_types: Mapped[list[SeekerIntendedVisaType]] = relationship(
        "SeekerIntendedVisaType", cascade="all, delete-orphan", lazy="selectin"
    )

    # Background
    education_level: Mapped[EducationLevel | None] = mapped_column(
        SAEnum(EducationLevel, name="education_level"), nullable=True
    )
    employment_status: Mapped[EmploymentStatus | None] = mapped_column(
        SAEnum(EmploymentStatus, name="employment_status"), nullable=True
    )
    employer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Financial (rough band used by AI eligibility — no precise figures stored)
    annual_income_band: Mapped[str | None] = mapped_column(String(50), nullable=True)
    has_bank_statements: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Notification preferences
    email_notifications: Mapped[bool] = mapped_column(default=True, nullable=False)
