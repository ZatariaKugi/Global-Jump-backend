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


class AcquisitionSourcePoint(BaseModel):
    """Acquisition pie slice — ``key`` for chart config, ``value`` for magnitude."""

    key: str
    label: str
    value: int


class GeoUsersPoint(BaseModel):
    """Users-by-country map row.

    ``country_code_numeric`` is the ISO 3166-1 numeric id (e.g. ``\"840\"``) used by
    map libraries; ``country_code`` is the alpha-2 we store on profiles.
    """

    country_code: str
    country_code_numeric: str
    country: str
    users: int


class RetentionSeriesPoint(BaseModel):
    """Per signup-cohort date: % retained at day 1 / 7 / 30 after registration.

    Percentages are null when the target day is still in the future.
    """

    date: str  # ISO YYYY-MM-DD (cohort signup date)
    day1: float | None
    day7: float | None
    day30: float | None


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
    revenue_today_usd: float
    booking_rate: float
    users_by_country: list[GeoUsersPoint]
    acquisition_sources: list[AcquisitionSourcePoint]
    onboarding_funnel: OnboardingFunnelRead
    retention: list[RetentionSeriesPoint]


# ── Advisor Analytics ────────────────────────────────────────────────────────


class TopAdvisorRead(BaseModel):
    user_id: uuid.UUID
    full_name: str | None
    email: str
    avatar_url: str | None
    avg_rating: float
    review_count: int


class SessionTrendPoint(BaseModel):
    """Completed consultation sessions per calendar month (absolute counts)."""

    month: str  # ISO "YYYY-MM"
    value: int


class AdvisorAnalyticsRead(BaseModel):
    window_days: int
    total_advisors: int
    session_completed_pct: float
    top_rated_advisors: list[TopAdvisorRead]
    session_trend: list[SessionTrendPoint]


# ── Finance Analytics ────────────────────────────────────────────────────────


class FinanceAnalyticsRead(BaseModel):
    window_days: int
    gross_revenue_usd: float
    net_revenue_usd: float
    refunds_usd: float
    advisor_payout_usd: float
    # % change vs the immediately preceding window of the same length.
    gross_revenue_change_pct: float
    net_revenue_change_pct: float
    refunds_change_pct: float
    advisor_payout_change_pct: float
    revenue_trend: list[MonthlyAmountPoint]
    refund_trend: list[MonthlyAmountPoint]
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
