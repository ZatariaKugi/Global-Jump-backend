"""Admin analytics dashboard — one aggregation function per tab (Python-side
date bucketing; no SQL date_trunc, see assessment_service.get_analytics)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity_log import ActivityLog
from app.models.advisor_lead import AdvisorLead
from app.models.assessment import Assessment, AssessmentStatus
from app.models.booking import Booking, BookingStatus
from app.models.message import Message
from app.models.payout_request import PayoutRequest, PayoutStatus
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.analytics import (
    AdvisorAnalyticsRead,
    AIAnalyticsRead,
    EligibilityTierMonthPoint,
    EngagementAnalyticsRead,
    FinanceAnalyticsRead,
    LabeledCountPoint,
    MonthlyAmountPoint,
    MonthlyCountPoint,
    OnboardingFunnelRead,
    OverviewAnalyticsRead,
    RetentionPoint,
    TopAdvisorRead,
)
from app.services import review_service

_MATCH_SCORE_BUCKETS = ["0-20", "20-40", "40-60", "60-80", "80-100"]
_DURATION_BUCKETS = ["<30m", "30-60m", "60-90m", "90m+"]


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _bucket_match_score(score: float) -> str:
    if score >= 100:
        return "80-100"
    index = min(int(score // 20), 4)
    return _MATCH_SCORE_BUCKETS[index]


def _bucket_duration(minutes: int) -> str:
    if minutes < 30:
        return "<30m"
    if minutes < 60:
        return "30-60m"
    if minutes < 90:
        return "60-90m"
    return "90m+"


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat stored values as UTC (mirrors
    availability_service.as_utc)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ── Overview ─────────────────────────────────────────────────────────────────


async def get_overview_analytics(session: AsyncSession, days: int = 30) -> OverviewAnalyticsRead:
    since = _since(days)

    total_users = (
        await session.execute(
            select(func.count()).select_from(User).where(User.created_at >= since)
        )
    ).scalar_one()
    total_advisors = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == UserRole.advisor, User.created_at >= since)
        )
    ).scalar_one()
    active_advisors = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.role == UserRole.advisor,
                User.is_active.is_(True),
                User.verification_status == VerificationStatus.approved,
            )
        )
    ).scalar_one()

    booked = (
        await session.execute(
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.status.in_([BookingStatus.confirmed, BookingStatus.completed]),
                Booking.scheduled_start >= since,
            )
        )
    ).scalar_one()
    assessed = (
        await session.execute(
            select(func.count())
            .select_from(Assessment)
            .where(Assessment.status == AssessmentStatus.completed, Assessment.created_at >= since)
        )
    ).scalar_one()
    booking_rate = round(100.0 * booked / assessed, 2) if assessed else 0.0

    country_rows = (
        await session.execute(
            select(SeekerProfile.country_of_residence, func.count())
            .join(User, User.id == SeekerProfile.user_id)
            .where(User.created_at >= since, SeekerProfile.country_of_residence.is_not(None))
            .group_by(SeekerProfile.country_of_residence)
        )
    ).all()
    users_by_country = [
        LabeledCountPoint(label=country, count=count) for country, count in country_rows
    ]

    source_rows = (
        await session.execute(
            select(User.signup_source, func.count())
            .where(User.created_at >= since)
            .group_by(User.signup_source)
        )
    ).all()
    acquisition_sources = [
        LabeledCountPoint(label=str(source), count=count) for source, count in source_rows
    ]

    onboarding_funnel = await _onboarding_funnel(session, since)
    retention = await _retention(session, since)

    return OverviewAnalyticsRead(
        window_days=days,
        total_users=total_users,
        total_advisors=total_advisors,
        active_advisors=active_advisors,
        booking_rate=booking_rate,
        users_by_country=users_by_country,
        acquisition_sources=acquisition_sources,
        onboarding_funnel=onboarding_funnel,
        retention=retention,
    )


async def _onboarding_funnel(session: AsyncSession, since: datetime) -> OnboardingFunnelRead:
    users = (await session.execute(select(User).where(User.created_at >= since))).scalars().all()
    registered = len(users)
    email_verified_users = [u for u in users if u.email_verified_at is not None]

    seeker_assessed = set(
        (
            await session.execute(
                select(Assessment.user_id).where(Assessment.status == AssessmentStatus.completed)
            )
        )
        .scalars()
        .all()
    )
    seeker_booked = set(
        (await session.execute(select(Booking.seeker_id).distinct())).scalars().all()
    )
    advisor_completed_booking = set(
        (
            await session.execute(
                select(Booking.advisor_id)
                .where(Booking.status == BookingStatus.completed)
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    activated_users = [
        u
        for u in email_verified_users
        if (u.role == UserRole.seeker and u.id in seeker_assessed)
        or (u.role == UserRole.advisor and u.verification_status == VerificationStatus.approved)
    ]
    engaged_users = [
        u
        for u in activated_users
        if (u.role == UserRole.seeker and u.id in seeker_booked)
        or (u.role == UserRole.advisor and u.id in advisor_completed_booking)
    ]

    return OnboardingFunnelRead(
        registered=registered,
        email_verified=len(email_verified_users),
        activated=len(activated_users),
        engaged=len(engaged_users),
    )


async def _retention(session: AsyncSession, since: datetime) -> list[RetentionPoint]:
    today = datetime.now(UTC).date()
    cohort_rows = (
        await session.execute(select(User.id, User.created_at).where(User.created_at >= since))
    ).all()
    cohorts: dict[date, list[uuid.UUID]] = defaultdict(list)
    for user_id, created_at in cohort_rows:
        cohorts[created_at.date()].append(user_id)

    activity_rows = (
        await session.execute(
            select(ActivityLog.user_id, ActivityLog.occurred_on).where(
                ActivityLog.occurred_on >= since.date()
            )
        )
    ).all()
    activity_by_user: dict[uuid.UUID, set[date]] = defaultdict(set)
    for user_id, occurred_on in activity_rows:
        activity_by_user[user_id].add(occurred_on)

    points: list[RetentionPoint] = []
    for n in (1, 7, 30):
        eligible_total = 0
        retained_total = 0
        for cohort_day, user_ids in cohorts.items():
            target_day = cohort_day + timedelta(days=n)
            if target_day > today:
                continue
            eligible_total += len(user_ids)
            for user_id in user_ids:
                if target_day in activity_by_user.get(user_id, set()):
                    retained_total += 1
        pct = round(100.0 * retained_total / eligible_total, 2) if eligible_total else 0.0
        points.append(RetentionPoint(day=n, retention_pct=pct))
    return points


# ── Advisor Analytics ────────────────────────────────────────────────────────


async def get_advisor_analytics(session: AsyncSession, days: int = 30) -> AdvisorAnalyticsRead:
    since = _since(days)

    total_advisors = (
        await session.execute(
            select(func.count()).select_from(User).where(User.role == UserRole.advisor)
        )
    ).scalar_one()

    advisor_ids = (
        (await session.execute(select(User.id).where(User.role == UserRole.advisor)))
        .scalars()
        .all()
    )
    summaries = await review_service.rating_summaries(session, list(advisor_ids))
    name_rows = (
        await session.execute(select(User.id, User.full_name).where(User.id.in_(advisor_ids)))
    ).all()
    names: dict[uuid.UUID, str | None] = {}
    for user_id, full_name in name_rows:
        names[user_id] = full_name
    top_rated_advisors = sorted(
        (
            TopAdvisorRead(
                user_id=advisor_id,
                full_name=names.get(advisor_id),
                avg_rating=avg,
                review_count=count,
            )
            for advisor_id, (avg, count) in summaries.items()
        ),
        key=lambda a: a.avg_rating,
        reverse=True,
    )

    bookings = (
        (
            await session.execute(
                select(Booking).where(
                    Booking.scheduled_start >= since, Booking.status != BookingStatus.pending
                )
            )
        )
        .scalars()
        .all()
    )
    completed = [b for b in bookings if b.status == BookingStatus.completed]
    session_completed_pct = round(100.0 * len(completed) / len(bookings), 2) if bookings else 0.0

    trend_counts: dict[str, int] = defaultdict(int)
    for booking in completed:
        trend_counts[_month_key(booking.scheduled_start)] += 1
    session_trend = [
        MonthlyCountPoint(month=month, count=count) for month, count in sorted(trend_counts.items())
    ]

    return AdvisorAnalyticsRead(
        window_days=days,
        total_advisors=total_advisors,
        session_completed_pct=session_completed_pct,
        top_rated_advisors=top_rated_advisors,
        session_trend=session_trend,
    )


# ── Finance Analytics ────────────────────────────────────────────────────────


async def get_finance_analytics(session: AsyncSession, days: int = 30) -> FinanceAnalyticsRead:
    since = _since(days)

    transactions = (
        (await session.execute(select(Transaction).where(Transaction.created_at >= since)))
        .scalars()
        .all()
    )
    gross_txns = [
        t
        for t in transactions
        if t.status
        in (
            TransactionStatus.succeeded,
            TransactionStatus.partially_refunded,
            TransactionStatus.refunded,
        )
    ]
    gross_revenue_usd = round(sum(t.amount_usd for t in gross_txns), 2)

    refunded_txns = [
        t for t in transactions if t.refunded_at is not None and _as_utc(t.refunded_at) >= since
    ]
    refunds_usd = round(sum(t.refunded_amount_usd or 0.0 for t in refunded_txns), 2)

    net_revenue_usd = round(gross_revenue_usd - refunds_usd, 2)

    revenue_trend_map: dict[str, float] = defaultdict(float)
    for t in gross_txns:
        revenue_trend_map[_month_key(t.created_at)] += t.amount_usd
    revenue_trend = [
        MonthlyAmountPoint(month=month, amount_usd=round(amount, 2))
        for month, amount in sorted(revenue_trend_map.items())
    ]

    payouts = (
        (
            await session.execute(
                select(PayoutRequest).where(
                    PayoutRequest.status == PayoutStatus.completed,
                    PayoutRequest.processed_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    advisor_payout_usd = round(sum(p.amount_usd for p in payouts), 2)

    payout_trend_map: dict[str, float] = defaultdict(float)
    for p in payouts:
        assert p.processed_at is not None  # filtered by processed_at >= since above
        payout_trend_map[_month_key(p.processed_at)] += p.amount_usd
    monthly_payouts = [
        MonthlyAmountPoint(month=month, amount_usd=round(amount, 2))
        for month, amount in sorted(payout_trend_map.items())
    ]

    return FinanceAnalyticsRead(
        window_days=days,
        gross_revenue_usd=gross_revenue_usd,
        net_revenue_usd=net_revenue_usd,
        refunds_usd=refunds_usd,
        advisor_payout_usd=advisor_payout_usd,
        revenue_trend=revenue_trend,
        monthly_payouts=monthly_payouts,
    )


# ── AI Analytics ─────────────────────────────────────────────────────────────


async def get_ai_analytics(session: AsyncSession, days: int = 30) -> AIAnalyticsRead:
    since = _since(days)

    status_rows = (
        await session.execute(
            select(AdvisorLead.status, func.count())
            .where(AdvisorLead.created_at >= since)
            .group_by(AdvisorLead.status)
        )
    ).all()
    recommendation_effectiveness = [
        LabeledCountPoint(label=str(status), count=count) for status, count in status_rows
    ]

    match_scores = (
        (
            await session.execute(
                select(AdvisorLead.match_score).where(AdvisorLead.created_at >= since)
            )
        )
        .scalars()
        .all()
    )
    match_bucket_counts: dict[str, int] = defaultdict(int)
    for score in match_scores:
        match_bucket_counts[_bucket_match_score(score)] += 1
    match_score_distribution = [
        LabeledCountPoint(label=label, count=match_bucket_counts.get(label, 0))
        for label in _MATCH_SCORE_BUCKETS
    ]

    durations = (
        (
            await session.execute(
                select(Booking.duration_minutes).where(
                    Booking.status == BookingStatus.completed, Booking.scheduled_start >= since
                )
            )
        )
        .scalars()
        .all()
    )
    duration_bucket_counts: dict[str, int] = defaultdict(int)
    for minutes in durations:
        duration_bucket_counts[_bucket_duration(minutes)] += 1
    session_duration_distribution = [
        LabeledCountPoint(label=label, count=duration_bucket_counts.get(label, 0))
        for label in _DURATION_BUCKETS
    ]

    assessment_rows = (
        await session.execute(
            select(Assessment.completed_at, Assessment.tier).where(
                Assessment.status == AssessmentStatus.completed,
                Assessment.completed_at >= since,
            )
        )
    ).all()
    tier_month_counts: dict[tuple[str, str], int] = defaultdict(int)
    for completed_at, tier in assessment_rows:
        assert completed_at is not None  # filtered by completed_at >= since above
        tier_month_counts[(_month_key(completed_at), str(tier))] += 1
    eligibility_assessments_trend = [
        EligibilityTierMonthPoint(month=month, tier=tier, count=count)
        for (month, tier), count in sorted(tier_month_counts.items())
    ]

    return AIAnalyticsRead(
        window_days=days,
        recommendation_effectiveness=recommendation_effectiveness,
        match_score_distribution=match_score_distribution,
        session_duration_distribution=session_duration_distribution,
        eligibility_assessments_trend=eligibility_assessments_trend,
    )


# ── Engagement Analytics ─────────────────────────────────────────────────────


async def get_engagement_analytics(
    session: AsyncSession, days: int = 30
) -> EngagementAnalyticsRead:
    since = _since(days)

    messages = (
        (
            await session.execute(
                select(Message)
                .where(Message.created_at >= since)
                .order_by(Message.conversation_id, Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    messages_sent = len(messages)

    messages_trend_map: dict[str, int] = defaultdict(int)
    for m in messages:
        messages_trend_map[_month_key(m.created_at)] += 1
    messages_sent_trend = [
        MonthlyCountPoint(month=month, count=count)
        for month, count in sorted(messages_trend_map.items())
    ]

    response_gaps_hours: list[float] = []
    prev_by_conversation: dict[uuid.UUID, Message] = {}
    for m in messages:
        prev = prev_by_conversation.get(m.conversation_id)
        if prev is not None and prev.sender_id != m.sender_id:
            gap = (m.created_at - prev.created_at).total_seconds() / 3600.0
            response_gaps_hours.append(gap)
        prev_by_conversation[m.conversation_id] = m
    avg_response_time_hours = (
        round(sum(response_gaps_hours) / len(response_gaps_hours), 2)
        if response_gaps_hours
        else 0.0
    )

    completed_bookings = (
        (
            await session.execute(
                select(Booking).where(
                    Booking.status == BookingStatus.completed, Booking.scheduled_start >= since
                )
            )
        )
        .scalars()
        .all()
    )
    session_completed = len(completed_bookings)

    completed_trend_map: dict[str, int] = defaultdict(int)
    sum_duration_map: dict[str, float] = defaultdict(float)
    count_duration_map: dict[str, int] = defaultdict(int)
    for b in completed_bookings:
        month = _month_key(b.scheduled_start)
        completed_trend_map[month] += 1
        sum_duration_map[month] += b.duration_minutes
        count_duration_map[month] += 1
    session_completed_trend = [
        MonthlyCountPoint(month=month, count=count)
        for month, count in sorted(completed_trend_map.items())
    ]
    video_call_hours_trend = [
        MonthlyAmountPoint(month=month, amount_usd=round(minutes / 60.0, 2))
        for month, minutes in sorted(sum_duration_map.items())
    ]
    session_duration_trend = [
        MonthlyAmountPoint(
            month=month,
            amount_usd=round((sum_duration_map[month] / count_duration_map[month]) / 60.0, 2),
        )
        for month in sorted(sum_duration_map)
    ]

    return EngagementAnalyticsRead(
        window_days=days,
        messages_sent=messages_sent,
        avg_response_time_hours=avg_response_time_hours,
        session_completed=session_completed,
        messages_sent_trend=messages_sent_trend,
        video_call_hours_trend=video_call_hours_trend,
        session_duration_trend=session_duration_trend,
        session_completed_trend=session_completed_trend,
    )
