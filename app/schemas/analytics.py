"""Admin analytics dashboard response schemas (one per tab)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

# ── shared point shapes ──────────────────────────────────────────────────────


class MonthlyCountPoint(BaseModel):
    month: str  # ISO "YYYY-MM"
    count: int


class MonthlyAmountPoint(BaseModel):
    month: str
    amount_usd: float


class LabeledCountPoint(BaseModel):
    label: str
    count: int


class RetentionPoint(BaseModel):
    day: int  # 1, 7, or 30
    retention_pct: float


# ── Overview ─────────────────────────────────────────────────────────────────


class OnboardingFunnelRead(BaseModel):
    registered: int
    email_verified: int
    activated: int
    engaged: int


class OverviewAnalyticsRead(BaseModel):
    window_days: int
    total_users: int
    total_advisors: int
    active_advisors: int
    booking_rate: float
    users_by_country: list[LabeledCountPoint]
    acquisition_sources: list[LabeledCountPoint]
    onboarding_funnel: OnboardingFunnelRead
    retention: list[RetentionPoint]


# ── Advisor Analytics ────────────────────────────────────────────────────────


class TopAdvisorRead(BaseModel):
    user_id: uuid.UUID
    full_name: str | None
    avg_rating: float
    review_count: int


class AdvisorAnalyticsRead(BaseModel):
    window_days: int
    total_advisors: int
    session_completed_pct: float
    top_rated_advisors: list[TopAdvisorRead]
    session_trend: list[MonthlyCountPoint]


# ── Finance Analytics ────────────────────────────────────────────────────────


class FinanceAnalyticsRead(BaseModel):
    window_days: int
    gross_revenue_usd: float
    net_revenue_usd: float
    refunds_usd: float
    advisor_payout_usd: float
    revenue_trend: list[MonthlyAmountPoint]
    monthly_payouts: list[MonthlyAmountPoint]


# ── AI Analytics ─────────────────────────────────────────────────────────────


class AssessmentVolumePoint(BaseModel):
    month: str  # axis label, e.g. "Jan"
    value: int  # assessment count for that month


class DropOffStagePoint(BaseModel):
    stage: str  # e.g. "Q1"
    value: float  # drop-off percentage (0–100) of assessments started


class AIAnalyticsRead(BaseModel):
    window_days: int
    pass_rate: float  # 0–100, % of completed assessments in pass tiers
    fail_rate: float  # 0–100, % of completed assessments in fail tiers
    assessment_volume: list[AssessmentVolumePoint]
    drop_off_points: list[DropOffStagePoint]


# ── Engagement Analytics ─────────────────────────────────────────────────────


class EngagementAnalyticsRead(BaseModel):
    window_days: int
    messages_sent: int
    avg_response_time_hours: float
    session_completed: int
    messages_sent_trend: list[MonthlyCountPoint]
    video_call_hours_trend: list[MonthlyAmountPoint]  # SUM(duration_minutes)/60 per month
    session_duration_trend: list[MonthlyAmountPoint]  # AVG(duration_minutes)/60 per month
    session_completed_trend: list[MonthlyCountPoint]
