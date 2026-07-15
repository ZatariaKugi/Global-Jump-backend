"""Advisor profile — professional details, specializations, pricing, and availability."""

from __future__ import annotations

import uuid

from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel


class AdvisorVisaSpecialization(Base):
    """One row per visa type an advisor specializes in."""

    __tablename__ = "advisor_visa_specializations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("advisor_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    specialization: Mapped[str] = mapped_column(String(50), nullable=False)


class AdvisorCountryExpertise(Base):
    """One row per country an advisor has expertise in."""

    __tablename__ = "advisor_country_expertise"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("advisor_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)


class AdvisorLanguage(Base):
    """One row per language an advisor speaks."""

    __tablename__ = "advisor_languages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("advisor_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    language: Mapped[str] = mapped_column(String(100), nullable=False)
    proficiency: Mapped[str] = mapped_column(String(20), nullable=False)


class AdvisorService(Base):
    """One row per service offering an advisor provides."""

    __tablename__ = "advisor_services"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("advisor_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service_type: Mapped[str] = mapped_column(String(100), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)


class AdvisorProfile(BaseModel):
    __tablename__ = "advisor_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Public identity
    title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    profile_photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Expertise
    years_of_experience: Mapped[int | None] = mapped_column(nullable=True)
    successful_applications: Mapped[int | None] = mapped_column(nullable=True)
    # Self-reported career success rate (0–100).
    successful_application_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Normalised into child tables
    visa_specializations: Mapped[list[AdvisorVisaSpecialization]] = relationship(
        "AdvisorVisaSpecialization", cascade="all, delete-orphan", lazy="selectin"
    )
    country_expertise: Mapped[list[AdvisorCountryExpertise]] = relationship(
        "AdvisorCountryExpertise", cascade="all, delete-orphan", lazy="selectin"
    )
    languages: Mapped[list[AdvisorLanguage]] = relationship(
        "AdvisorLanguage", cascade="all, delete-orphan", lazy="selectin"
    )
    services: Mapped[list[AdvisorService]] = relationship(
        "AdvisorService", cascade="all, delete-orphan", lazy="selectin"
    )

    # Booking policy (PRD §3.6: cancellation policy configured per advisor)
    cancellation_notice_hours: Mapped[int] = mapped_column(
        default=24, server_default="24", nullable=False
    )

    # Admin-managed flags
    is_featured: Mapped[bool] = mapped_column(default=False, nullable=False)
    public_profile_slug: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)

    # Stripe Connect — set when advisor completes payout onboarding
    stripe_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
