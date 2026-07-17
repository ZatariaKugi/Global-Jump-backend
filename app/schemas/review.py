"""Schemas for ratings & reviews (PRD §3.9)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.review import ModerationStatus


class ReviewCreate(BaseModel):
    rating_expertise: int = Field(ge=1, le=5)
    rating_communication: int = Field(ge=1, le=5)
    rating_professionalism: int = Field(ge=1, le=5)
    rating_value: int = Field(ge=1, le=5)
    text: str | None = Field(default=None, max_length=500)


class ReviewResponseCreate(BaseModel):
    response: str = Field(min_length=1, max_length=500)


class ReviewReport(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ModerationDecision(BaseModel):
    action: str = Field(pattern="^(approve|remove)$")


class ReviewRead(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    advisor_id: uuid.UUID
    seeker_name: str | None
    seeker_photo_url: str | None = None
    seeker_subtitle: str | None = None  # display label, e.g. "Study Visa"
    rating_expertise: int
    rating_communication: int
    rating_professionalism: int
    rating_value: int
    rating_overall: float
    text: str | None
    is_verified: bool
    advisor_response: str | None
    responded_at: datetime | None
    created_at: datetime


class ReviewAdminRead(ReviewRead):
    seeker_id: uuid.UUID
    moderation_status: ModerationStatus
    flag_reason: str | None


class AdvisorRatingSummary(BaseModel):
    average_rating: float | None
    review_count: int


class RatingStarBreakdown(BaseModel):
    stars: int  # 1–5
    count: int


class AdvisorReviewSummaryRead(BaseModel):
    """Top RatingSummaryCard for the admin/advisor Reviews tab."""

    overall: float | None
    review_count: int
    positive_percent: float | None  # % with overall >= 4.0
    breakdown: list[RatingStarBreakdown]


class AdvisorReviewsTabRead(BaseModel):
    """Reviews tab payload — summary cards + paginated rows."""

    summary: AdvisorReviewSummaryRead
    items: list[ReviewRead]
