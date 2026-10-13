"""Seeker home dashboard aggregate schemas (FE ``/seeker/dashboard``).

A single home-screen summary for a seeker — stat cards, the per-stage visa
journey chart, the eligibility donut, and AI-matched advisors.

``visa_type`` is an explicit filter only (same as GET /users/me/documents):
omitted = all visa types; a specific value scopes stats, journey, eligibility,
matched advisors, and ``documents_uploaded`` (untagged docs included).
``days`` (7 / 30 / 90 / 180 / 365; omitted = all-time) is independent and
combines with visa when both are set — it windows eligibility to the latest
completed assessment in-window (score 0 if none) and ``documents_uploaded``
to in-window uploads. Journey stages, progress percents, ``next_upcoming``,
and ``matched_advisors`` / ``ai_recommended_advisors`` (profile-based
``seeker_advisor_recommendations``, every match vs. the paid AI shortlist)
remain current-state. ``window_days`` echoes the applied window (null = all-time).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.core.visa_types import OptionalVisaType
from app.models.assessment import EligibilityTier
from app.schemas.assessment import AdvisorMatchRead, AiMatchStatusRead
from app.schemas.booking import BookingRead
from app.schemas.visa_journey import JourneyStepKey, JourneyStepStatus


class SeekerDashboardStats(BaseModel):
    """Stat-card figures. ``eligibility_score`` is 0 when no completed
    assessment exists (including an empty ``days`` window). ``visa_type`` /
    ``country`` echo the request filters (null when omitted — never inferred).
    ``documents_uploaded`` matches GET /users/me/documents: all visas when
    ``visa_type`` is omitted, that visa plus untagged docs when set;
    in-window only when ``days`` is set."""

    eligibility_score: float
    eligibility_tier: EligibilityTier | None
    visa_type: OptionalVisaType
    visa_type_name: str | None
    country: str | None = Field(description="ISO 3166-1 alpha-2")
    country_name: str | None
    journey_progress_percent: int
    documents_uploaded: int
    documents_progress_percent: int
    application_status: JourneyStepStatus
    application_status_percent: int


class JourneyStageRead(BaseModel):
    """One bar on the Visa Journey Timeline chart (Profile Based Recommendation →
    Advisor → Documents → Submission). ``progress_percent`` is derived from step
    status (completed=100, in_progress=50, pending=0) except documentation,
    which uses the real document-checklist percent. Review is not exposed here.
    """

    key: JourneyStepKey
    label: str
    status: JourneyStepStatus
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
    window_days: int | None = None
    next_upcoming: BookingRead | None
    stats: SeekerDashboardStats
    journey_stages: list[JourneyStageRead]
    eligibility_breakdown: EligibilityBreakdownRead | None
    # Every advisor the profile matching ranked, paying or not (the "Matched
    # Advisors" tab; booking is never a plan feature).
    matched_advisors: list[AdvisorMatchRead]
    # The premium shortlist ("AI Recommended" tab): advisors whose own plan grants
    # ``ai_recommended``, sent only when the seeker's plan includes ``matched_advisors``.
    ai_recommended_advisors: list[AdvisorMatchRead] = Field(default_factory=list)
    # True when the seeker's plan lacks ``matched_advisors``: the client shows the tab
    # locked with an upgrade message instead of an empty list.
    ai_recommendations_locked: bool = False
    assessment_id: uuid.UUID | None
    ai_match: AiMatchStatusRead | None = None
