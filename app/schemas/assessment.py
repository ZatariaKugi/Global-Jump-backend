"""Schemas for the AI eligibility assessment engine (PRD §3.4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.core.visa_types import OptionalVisaType, RequiredVisaType
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
    category: QuestionCategory | None
    display_order: int
    depends_on_option_id: uuid.UUID | None
    options: list[QuestionOptionRead]


# ── Admin question config ────────────────────────────────────────────────────


class QuestionOptionInput(BaseModel):
    text: str = Field(min_length=1, max_length=255)
    score: float = Field(ge=0, le=100)
    improvement_tip: str | None = Field(default=None, max_length=500)
    display_order: int = 0


class QuestionOptionPatchInput(BaseModel):
    """PATCH option row — merge by ``id`` when present; create when omitted."""

    id: uuid.UUID | None = None
    text: str | None = Field(default=None, min_length=1, max_length=255)
    score: float | None = Field(default=None, ge=0, le=100)
    improvement_tip: str | None = Field(default=None, max_length=500)
    display_order: int | None = None

    @model_validator(mode="after")
    def _create_requires_text(self) -> QuestionOptionPatchInput:
        if self.id is None and self.text is None:
            raise ValueError("text is required when creating a new option (id omitted)")
        return self


class QuestionCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    # Optional — questions are scoped by country/visa_type; category is for
    # result breakdown only when provided.
    category: QuestionCategory | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    weight: float = Field(default=1.0, gt=0, le=10)
    # UI "Weightage %" (0–100) — preferred; converts to ``weight`` = pct / 10.
    weightage_pct: float | None = Field(default=None, ge=0, le=100)
    display_order: int = 0
    is_active: bool = True
    depends_on_option_id: uuid.UUID | None = None
    options: list[QuestionOptionInput] = Field(min_length=2)

    @model_validator(mode="after")
    def _apply_weightage_pct(self) -> QuestionCreate:
        if self.weightage_pct is not None:
            self.weight = max(0.1, round(self.weightage_pct / 10.0, 2))
        return self


class QuestionUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    category: QuestionCategory | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    visa_type: OptionalVisaType = None
    weight: float | None = Field(default=None, gt=0, le=10)
    weightage_pct: float | None = Field(default=None, ge=0, le=100)
    display_order: int | None = None
    is_active: bool | None = None
    depends_on_option_id: uuid.UUID | None = None
    # Merge semantics: update by id, create when id absent, delete when omitted.
    options: list[QuestionOptionPatchInput] | None = None

    @model_validator(mode="after")
    def _apply_weightage_pct(self) -> QuestionUpdate:
        if self.weightage_pct is not None:
            self.weight = max(0.1, round(self.weightage_pct / 10.0, 2))
        return self


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
    category: QuestionCategory | None
    country_code: str | None
    visa_type: OptionalVisaType
    weight: float
    weightage_pct: float
    display_order: int
    is_active: bool
    depends_on_option_id: uuid.UUID | None
    options: list[QuestionOptionAdminRead]


# ── Assessment sessions ──────────────────────────────────────────────────────


class AssessmentCreate(BaseModel):
    destination_country: str = Field(min_length=2, max_length=2)
    visa_type: RequiredVisaType


class AnswerInput(BaseModel):
    question_id: uuid.UUID
    option_id: uuid.UUID


class AnswersSubmit(BaseModel):
    answers: list[AnswerInput] = Field(min_length=1)
    # False = save progress only (enables drop-off-by-question analytics).
    complete: bool = True


class CategoryScoreRead(BaseModel):
    category: str
    score: float


class AdvisorMatchRead(BaseModel):
    user_id: uuid.UUID
    full_name: str | None
    email: str | None = None
    title: str | None
    profile_photo_url: str | None
    years_of_experience: int | None
    average_rating: float | None = None
    starting_price_usd: float | None = None
    match_score: float
    public_profile_slug: str | None


class AssessmentRead(BaseModel):
    id: uuid.UUID
    destination_country: str
    visa_type: RequiredVisaType
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
    visa_type: RequiredVisaType
    status: AssessmentStatus
    score: float | None
    tier: EligibilityTier | None
    created_at: datetime
    completed_at: datetime | None


# ── AI Analytics (PRD §3.4 AI Engine Management) ────────────────────────────


class AssessmentVolumePoint(BaseModel):
    """Area-chart point — month axis label + assessment count."""

    month: str  # e.g. "Jan"
    value: int


class AssessmentDropOffPoint(BaseModel):
    """Bar-chart point — question stage + drop-off % of assessments started."""

    stage: str  # e.g. "Q1", "Q2"
    value: float


class AssessmentAnalyticsRead(BaseModel):
    window_days: int
    pass_rate: float  # 0–100
    fail_rate: float  # 0–100
    assessment_volume: list[AssessmentVolumePoint]
    drop_off_points: list[AssessmentDropOffPoint]
