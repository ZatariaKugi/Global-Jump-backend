"""AI visa eligibility assessment engine — questions, sessions, and results (PRD §3.4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.base_model import BaseModel


class QuestionCategory(StrEnum):
    nationality = "nationality"
    travel_history = "travel_history"
    financial = "financial"
    education = "education"
    employment = "employment"
    criminal_record = "criminal_record"
    visa_refusals = "visa_refusals"
    family_ties = "family_ties"
    language = "language"
    purpose = "purpose"


class AssessmentStatus(StrEnum):
    in_progress = "in_progress"
    completed = "completed"


class EligibilityTier(StrEnum):
    highly_eligible = "highly_eligible"
    likely_eligible = "likely_eligible"
    borderline = "borderline"
    low_eligibility = "low_eligibility"


class AssessmentQuestionOption(Base):
    """One selectable answer for a question, with its score contribution."""

    __tablename__ = "assessment_question_options"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)  # 0–100 contribution
    # Shown in the result when this (low-scoring) option was selected.
    improvement_tip: Mapped[str | None] = mapped_column(String(500), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AssessmentQuestion(BaseModel):
    """Admin-configurable questionnaire item, optionally scoped per country/visa type."""

    __tablename__ = "assessment_questions"

    text: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    category: Mapped[QuestionCategory | None] = mapped_column(
        SAEnum(QuestionCategory, name="question_category"), nullable=True
    )
    # NULL = applies to all countries / visa types.
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    visa_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Adaptive questionnaire: only asked when a previous answer selected this option.
    # Bare UUID (no FK) to avoid a circular table dependency; validated in the service.
    depends_on_option_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    options: Mapped[list[AssessmentQuestionOption]] = relationship(
        "AssessmentQuestionOption",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="AssessmentQuestionOption.display_order",
    )


class AssessmentAnswer(Base):
    """One answered question within an assessment."""

    __tablename__ = "assessment_answers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_questions.id", ondelete="CASCADE"), nullable=False
    )
    option_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_question_options.id", ondelete="CASCADE"), nullable=False
    )


class AssessmentCategoryScore(Base):
    """Per-category breakdown row of a completed assessment."""

    __tablename__ = "assessment_category_scores"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)


class AssessmentTip(Base):
    """Improvement tip attached to a completed assessment."""

    __tablename__ = "assessment_tips"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tip: Mapped[str] = mapped_column(String(500), nullable=False)


class InsightKind(StrEnum):
    strength = "strength"
    weakness = "weakness"
    missing_requirement = "missing_requirement"


class AssessmentInsight(Base):
    """AI-generated narrative insight attached to a completed assessment.

    Absent rows mean the AI was unavailable at completion — the frontend
    falls back to the pre-authored improvement tips.
    """

    __tablename__ = "assessment_insights"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[InsightKind] = mapped_column(
        SAEnum(InsightKind, name="insight_kind"), nullable=False
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Assessment(BaseModel):
    """A seeker's eligibility assessment session and its result."""

    __tablename__ = "assessments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    destination_country: Mapped[str] = mapped_column(String(2), nullable=False)
    visa_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[AssessmentStatus] = mapped_column(
        SAEnum(AssessmentStatus, name="assessment_status"),
        default=AssessmentStatus.in_progress,
        nullable=False,
    )

    # Result — populated on completion.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[EligibilityTier | None] = mapped_column(
        SAEnum(EligibilityTier, name="eligibility_tier"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0–1
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # AI-generated improvement summary — NULL when AI was unavailable at completion.
    ai_summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Optional A/B experiment arm assigned at assessment start.
    ab_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessment_ab_variants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    answers: Mapped[list[AssessmentAnswer]] = relationship(
        "AssessmentAnswer", cascade="all, delete-orphan", lazy="selectin"
    )
    category_scores: Mapped[list[AssessmentCategoryScore]] = relationship(
        "AssessmentCategoryScore", cascade="all, delete-orphan", lazy="selectin"
    )
    tips: Mapped[list[AssessmentTip]] = relationship(
        "AssessmentTip", cascade="all, delete-orphan", lazy="selectin"
    )
    insights: Mapped[list[AssessmentInsight]] = relationship(
        "AssessmentInsight",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="AssessmentInsight.display_order",
    )
