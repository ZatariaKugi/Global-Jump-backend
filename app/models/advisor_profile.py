"""Advisor profile — professional details, specializations, pricing, and availability."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
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
    """One row per country an advisor serves / has expertise in."""

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


class AdvisorOfferedService(Base):
    __tablename__ = "advisor_offered_services"
    __table_args__ = (
        UniqueConstraint("profile_id", "service_id", name="uq_offered_service_profile_service"),
        Index(
            "uq_offered_service_catalog_name",
            "name",
            unique=True,
            postgresql_where=text("profile_id IS NULL"),
            sqlite_where=text("profile_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("advisor_profiles.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("advisor_offered_services.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )

    catalog_service: Mapped[AdvisorOfferedService | None] = relationship(
        "AdvisorOfferedService",
        remote_side="AdvisorOfferedService.id",
        foreign_keys=[service_id],
        uselist=False,
        lazy="joined",
    )




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
    banner_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Onboarding location + free-text expertise description
    country_of_residence: Mapped[str | None] = mapped_column(String(2), nullable=True)
    expertise_description: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Contact & location
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # IANA timezone (e.g. "Asia/Karachi") and free-text education / degree.
    # Advisor-editable; shown on the admin Overview tab.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    education: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Expertise
    years_of_experience: Mapped[int | None] = mapped_column(nullable=True)
    successful_applications: Mapped[int | None] = mapped_column(nullable=True)
    # Self-reported career success rate (0–100). Independent of the count above.
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
    offered_services: Mapped[list[AdvisorOfferedService]] = relationship(
        "AdvisorOfferedService", cascade="all, delete-orphan", lazy="selectin"
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
    # Cached Connect account readiness (refreshed from the account.updated webhook
    # and get_connect_status) so checkout can gate on payout-readiness without a
    # live Stripe API round-trip.
    stripe_charges_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    stripe_payouts_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    stripe_details_submitted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # Integration onboarding banners — synced when Stripe/Zoom state changes.
    needs_stripe_connect: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    needs_zoom_connect: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
