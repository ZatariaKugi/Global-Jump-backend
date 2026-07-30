"""Seeker home dashboard aggregate schemas (FE ``/seeker/dashboard``).

A single home-screen summary for a seeker — stat cards, the per-stage visa
journey chart, the eligibility donut, and AI-matched advisors. Every figure is
current-state (no ``days`` window): eligibility, journey progress, and document
tallies don't change meaning over a 7/30/90-day range.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.core.visa_types import OptionalVisaType
from app.models.assessment import EligibilityTier
from app.schemas.assessment import AdvisorMatchRead
from app.schemas.booking import BookingRead
from app.schemas.visa_journey import JourneyStepKey


class SeekerDashboardStats(BaseModel):
    """Stat-card figures. ``eligibility_*`` come from the latest completed
    assessment for the resolved visa/country scope — null until one exists."""

    eligibility_score: float | None
    eligibility_tier: EligibilityTier | None
    visa_type: OptionalVisaType
    visa_type_name: str | None
    country: str | None = Field(description="ISO 3166-1 alpha-2")
    country_name: str | None
    journey_progress_percent: int
    documents_uploaded: int
    documents_progress_percent: int
    application_status_percent: int


class JourneyStageRead(BaseModel):
    """One bar on the Visa Journey Timeline chart. ``progress_percent`` is
    derived from step status (completed=100, in_progress=50, pending=0) except
    documentation, which uses the real document-checklist percent."""

    key: JourneyStepKey
    label: str
    progress_percent: int
    active: bool


class EligibilityBreakdownSegment(BaseModel):
    key: Literal["eligibility", "missing_requirements"]
    label: str
    value: int  # 0–100; the two segments sum to 100


class EligibilityBreakdownRead(BaseModel):
    """Donut — segments from the assessment score, center = journey cover."""

    center_percent: int
    segments: list[EligibilityBreakdownSegment]


class SeekerDashboardRead(BaseModel):
    next_upcoming: BookingRead | None
    stats: SeekerDashboardStats
    journey_stages: list[JourneyStageRead]
    eligibility_breakdown: EligibilityBreakdownRead | None  
    matched_advisors: list[AdvisorMatchRead]  
    assessment_id: uuid.UUID | None  
