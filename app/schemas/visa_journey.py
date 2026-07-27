"""Schemas for the seeker Visa Journey Tracking dashboard."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.core.visa_types import OptionalVisaType


class JourneyStepKey(StrEnum):
    assessment = "assessment"
    advisor = "advisor"
    documentation = "documentation"
    application_preparation = "application_preparation"
    submission = "submission"


class JourneyStepStatus(StrEnum):
    completed = "completed"
    in_progress = "in_progress"
    pending = "pending"


class VisaJourneyStep(BaseModel):
    key: JourneyStepKey
    title: str
    description: str
    status: JourneyStepStatus
    order: int = Field(ge=1)
    action: str | None = None
    action_label: str | None = None


class AdvisorSuggestion(BaseModel):
    advisor_id: uuid.UUID
    advisor_name: str | None
    advisor_photo_url: str | None
    body: str
    source: Literal["document_request", "message", "checklist"]
    created_at: datetime | None = None


class VisaJourneyRead(BaseModel):
    visa_type: OptionalVisaType
    visa_type_name: str | None
    country: str | None = Field(description="ISO 3166-1 alpha-2")
    country_name: str | None
    current_status: str
    current_status_label: str
    completed_steps: int
    total_steps: int
    progress_percent: int
    steps: list[VisaJourneyStep]
    advisor_suggestion: AdvisorSuggestion | None
    assessment_id: uuid.UUID | None = None
    booking_id: uuid.UUID | None = None
