"""Schemas for the AI eligibility assessment engine (PRD §3.4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.assessment import AssessmentStatus, EligibilityTier, QuestionCategory

# ── Questionnaire ────────────────────────────────────────────────────────────


class QuestionOptionRead(BaseModel):
    id: uuid.UUID
    text: str
    display_order: int


class QuestionRead(BaseModel):
    id: uuid.UUID
    text: str
    description: str | None
    category: QuestionCategory
    display_order: int
    depends_on_option_id: uuid.UUID | None
    options: list[QuestionOptionRead]


# ── Admin question config ────────────────────────────────────────────────────


class QuestionOptionInput(BaseModel):
    text: str = Field(min_length=1, max_length=255)
    score: float = Field(ge=0, le=100)
    improvement_tip: str | None = Field(default=None, max_length=500)
    display_order: int = 0


class QuestionCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    category: QuestionCategory
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: str | None = Field(default=None, max_length=50)
    weight: float = Field(default=1.0, gt=0, le=10)
    display_order: int = 0
    is_active: bool = True
    depends_on_option_id: uuid.UUID | None = None
    options: list[QuestionOptionInput] = Field(min_length=2)


class QuestionUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    category: QuestionCategory | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: str | None = Field(default=None, max_length=50)
    weight: float | None = Field(default=None, gt=0, le=10)
    display_order: int | None = None
    is_active: bool | None = None
    depends_on_option_id: uuid.UUID | None = None
    options: list[QuestionOptionInput] | None = None


class QuestionOptionAdminRead(BaseModel):
    id: uuid.UUID
    text: str
    score: float
    improvement_tip: str | None
    display_order: int


class QuestionAdminRead(BaseModel):
    id: uuid.UUID
    text: str
    description: str | None
    category: QuestionCategory
    country_code: str | None
    visa_type: str | None
    weight: float
    display_order: int
    is_active: bool
    depends_on_option_id: uuid.UUID | None
    options: list[QuestionOptionAdminRead]


# ── Assessment sessions ──────────────────────────────────────────────────────


class AssessmentCreate(BaseModel):
    destination_country: str = Field(min_length=2, max_length=2)
    visa_type: str = Field(min_length=1, max_length=50)


class AnswerInput(BaseModel):
    question_id: uuid.UUID
    option_id: uuid.UUID


class AnswersSubmit(BaseModel):
    answers: list[AnswerInput] = Field(min_length=1)


class CategoryScoreRead(BaseModel):
    category: str
    score: float


class AdvisorMatchRead(BaseModel):
    user_id: uuid.UUID
    full_name: str | None
    title: str | None
    profile_photo_url: str | None
    years_of_experience: int | None
    match_score: float
    public_profile_slug: str | None


class AssessmentRead(BaseModel):
    id: uuid.UUID
    destination_country: str
    visa_type: str
    status: AssessmentStatus
    score: float | None
    tier: EligibilityTier | None
    confidence: float | None
    created_at: datetime
    completed_at: datetime | None
    category_scores: list[CategoryScoreRead]
    improvement_tips: list[str]
    strengths: list[str]
    weaknesses: list[str]
    missing_requirements: list[str]
    ai_summary: str | None
    matched_advisors: list[AdvisorMatchRead]


class AssessmentSummaryRead(BaseModel):
    """Compact row for assessment history listings."""

    id: uuid.UUID
    destination_country: str
    visa_type: str
    status: AssessmentStatus
    score: float | None
    tier: EligibilityTier | None
    created_at: datetime
    completed_at: datetime | None


# ── AI Analytics (PRD §3.4 AI Engine Management) ────────────────────────────


class AssessmentVolumePoint(BaseModel):
    date: str  # ISO date (YYYY-MM-DD)
    count: int


class AssessmentAnalyticsRead(BaseModel):
    window_days: int
    total_started: int
    total_completed: int
    volume: list[AssessmentVolumePoint]
    pass_rate: float
    fail_rate: float
    # Assessments started but never completed, within the window — a coarse
    # proxy for "drop off": there's no incremental per-question save today, so
    # this can't say *which* question someone abandoned on, only that they did.
    drop_off_count: int
    drop_off_rate: float
