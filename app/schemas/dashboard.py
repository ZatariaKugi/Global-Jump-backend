"""Admin dashboard home screen response schemas.

Distinct from analytics.py (per-tab deep-dive analytics) — this is the
single home-screen summary. Reuses MonthlyCountPoint from analytics.py
rather than redefining it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app.schemas.analytics import MonthlyCountPoint


class ActivityEventType(StrEnum):
    new_user_registered = "new_user_registered"
    advisor_application_submitted = "advisor_application_submitted"
    session_completed = "session_completed"
    refund_request = "refund_request"
    review_flagged = "review_flagged"
    document_uploaded = "document_uploaded"


class RevenueBreakdownSliceRead(BaseModel):
    label: str  # "Consultant" | "Document Review" | "Others"
    amount_usd: float
    pct: float  # 0-100, 2dp; slices sum to ~100 modulo rounding


class ActivityFeedItemRead(BaseModel):
    event_type: ActivityEventType
    occurred_at: datetime
    title: str
    description: str


class DashboardSummaryRead(BaseModel):
    window_days: int
    total_users: int  # all-time, NOT windowed
    total_advisors: int  # all-time
    active_advisors: int  # all-time
    revenue_today_usd: float  # today's UTC calendar date only, unaffected by window_days
    user_registration_trend: list[MonthlyCountPoint]  # all users, monthly
    ai_assessment_volume: list[MonthlyCountPoint]  # all assessments, monthly
    revenue_breakdown: list[RevenueBreakdownSliceRead]  # empty buckets omitted
    recent_activities: list[ActivityFeedItemRead]  # capped at 6
