"""Admin analytics dashboard — one aggregation function per tab (Python-side
date bucketing; no SQL date_trunc, see assessment_service.get_analytics)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.countries import country_name, country_numeric
from app.core.visa_types import VISA_TYPE_LABELS, parse_visa_type
from app.models.activity_log import ActivityLog
from app.models.advisor_lead import AdvisorLead
from app.models.advisor_profile import AdvisorProfile
from app.models.assessment import Assessment, AssessmentStatus, EligibilityTier
from app.models.booking import Booking, BookingStatus
from app.models.message import Message
from app.models.payout_request import PayoutRequest, PayoutStatus
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole, VerificationStatus
from app.models.visa_type import VisaType
from app.schemas.analytics import (
    AcquisitionSourcePoint,
    AdvisorAnalyticsRead,
    AdvisorMatchFunnelPoint,
    AIAnalyticsRead,
    AssessmentDistributionPoint,
    EligibilityBreakdownPoint,
    EngagementAnalyticsRead,
    FinanceAnalyticsRead,
    GeoUsersPoint,
    MonthlyAmountPoint,
    MonthlyCountPoint,
    OnboardingFunnelRead,
    OverviewAnalyticsRead,
    RetentionSeriesPoint,
    SessionTrendPoint,
    TopAdvisorRead,
)
from app.services import review_service

# Donut / stacked-bar order matches the admin AI analytics FE.
_AI_VISA_ORDER: tuple[VisaType, ...] = (
    VisaType.student,
    VisaType.work,
    VisaType.tourist,
    VisaType.pr,
    VisaType.family,
    VisaType.investment,
    VisaType.asylum,
)

_ELIGIBILITY_HIGH = frozenset({EligibilityTier.highly_eligible})
_ELIGIBILITY_MEDIUM = frozenset({EligibilityTier.likely_eligible})
_ELIGIBILITY_LOW = frozenset(
    {EligibilityTier.borderline, EligibilityTier.low_eligibility}
)

_FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("impressions", "Impressions"),
    ("matches_shown", "Matches Shown"),
    ("advisors_clicked", "Advisors Clicked"),
    ("session_booked", "Session Booked"),
)

_SESSION_BOOKED_STATUSES = frozenset(
    {BookingStatus.confirmed, BookingStatus.completed}
)

_GROSS_STATUSES = (
    TransactionStatus.succeeded,
    TransactionStatus.partially_refunded,
    TransactionStatus.refunded,
)


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _slug_key(label: str) -> str:
    """Stable chart key from a free-text label (e.g. paid_ads → paid_ads)."""
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in label.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "unknown"


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

    today = datetime.now(UTC).date()
    today_start = datetime(today.year, today.month, today.day, tzinfo=UTC)
    today_end = today_start + timedelta(days=1)
    revenue_today_usd = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.amount_usd), 0.0)).where(
                Transaction.status.in_(_GROSS_STATUSES),
                Transaction.created_at >= today_start,
                Transaction.created_at < today_end,
            )
        )
    ).scalar_one()

    country_rows = (
        await session.execute(
            select(SeekerProfile.country_of_residence, func.count())
            .join(User, User.id == SeekerProfile.user_id)
            .where(User.created_at >= since, SeekerProfile.country_of_residence.is_not(None))
            .group_by(SeekerProfile.country_of_residence)
        )
    ).all()
    users_by_country: list[GeoUsersPoint] = []
    for code, count in country_rows:
        alpha = str(code).upper()
        numeric = country_numeric(alpha)
        if numeric is None:
            continue
        users_by_country.append(
            GeoUsersPoint(
                country_code=alpha,
                country_code_numeric=numeric,
                country=country_name(alpha) or alpha,
                users=count,
            )
        )

    source_rows = (
        await session.execute(
            select(User.signup_source, func.count())
            .where(User.created_at >= since)
            .group_by(User.signup_source)
        )
    ).all()
    acquisition_sources = [
        AcquisitionSourcePoint(
            key=_slug_key(str(source)),
            label=str(source),
            value=count,
        )
        for source, count in source_rows
    ]

    onboarding_funnel = await _onboarding_funnel(session, since)
    retention = await _retention(session, since)

    return OverviewAnalyticsRead(
        window_days=days,
        total_users=total_users,
        total_advisors=total_advisors,
        active_advisors=active_advisors,
        revenue_today_usd=round(float(revenue_today_usd), 2),
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


async def _retention(session: AsyncSession, since: datetime) -> list[RetentionSeriesPoint]:
    """Per signup-date cohort: day1 / day7 / day30 return rates.

    Each point is one registration calendar day in the window. Percentages are
    null when the target day is still in the future (cohort too young).
    """
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

    def _pct(cohort_day: date, n: int, user_ids: list[uuid.UUID]) -> float | None:
        target = cohort_day + timedelta(days=n)
        if target > today:
            return None
        if not user_ids:
            return 0.0
        retained = sum(1 for uid in user_ids if target in activity_by_user.get(uid, set()))
        return round(100.0 * retained / len(user_ids), 2)

    points: list[RetentionSeriesPoint] = []
    for cohort_day in sorted(cohorts):
        user_ids = cohorts[cohort_day]
        points.append(
            RetentionSeriesPoint(
                date=cohort_day.isoformat(),
                day1=_pct(cohort_day, 1, user_ids),
                day7=_pct(cohort_day, 7, user_ids),
                day30=_pct(cohort_day, 30, user_ids),
            )
        )
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
    advisor_rows = (
        await session.execute(
            select(User.id, User.full_name, User.email, AdvisorProfile.profile_photo_url)
            .outerjoin(AdvisorProfile, AdvisorProfile.user_id == User.id)
            .where(User.id.in_(advisor_ids))
        )
    ).all()
    advisors_by_id: dict[uuid.UUID, tuple[str | None, str, str | None]] = {
        user_id: (full_name, email, photo) for user_id, full_name, email, photo in advisor_rows
    }
    top_rated_advisors = sorted(
        (
            TopAdvisorRead(
                user_id=advisor_id,
                full_name=full_name,
                email=email,
                avatar_url=photo,
                avg_rating=avg,
                review_count=count,
            )
            for advisor_id, (avg, count) in summaries.items()
            if (info := advisors_by_id.get(advisor_id)) is not None
            for full_name, email, photo in (info,)
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
        SessionTrendPoint(month=month, value=count) for month, count in sorted(trend_counts.items())
    ]

    return AdvisorAnalyticsRead(
        window_days=days,
        total_advisors=total_advisors,
        session_completed_pct=session_completed_pct,
        top_rated_advisors=top_rated_advisors,
        session_trend=session_trend,
    )


# ── Finance Analytics ────────────────────────────────────────────────────────


def _change_pct(current: float, previous: float) -> float:
    """Percent change of ``current`` vs ``previous`` (0 when both zero)."""
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return round(100.0 * (current - previous) / previous, 2)


def _finance_window_totals(
    transactions: Sequence[Transaction],
    payouts: Sequence[PayoutRequest],
    window_start: datetime,
    window_end: datetime,
) -> tuple[float, float, float, float]:
    """Gross / refunds / net / advisor payout for [window_start, window_end)."""
    gross_txns = [
        t
        for t in transactions
        if t.status in _GROSS_STATUSES
        and window_start <= _as_utc(t.created_at) < window_end
    ]
    gross = round(sum(t.amount_usd for t in gross_txns), 2)

    refunded = [
        t
        for t in transactions
        if t.refunded_at is not None and window_start <= _as_utc(t.refunded_at) < window_end
    ]
    refunds = round(sum(t.refunded_amount_usd or 0.0 for t in refunded), 2)
    net = round(gross - refunds, 2)

    window_payouts = [
        p
        for p in payouts
        if p.processed_at is not None and window_start <= _as_utc(p.processed_at) < window_end
    ]
    advisor_payout = round(sum(p.amount_usd for p in window_payouts), 2)
    return gross, refunds, net, advisor_payout


async def get_finance_analytics(session: AsyncSession, days: int = 30) -> FinanceAnalyticsRead:
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    prev_since = now - timedelta(days=2 * days)

    # Load both current and previous windows in one pass.
    transactions = (
        (
            await session.execute(
                select(Transaction).where(Transaction.created_at >= prev_since)
            )
        )
        .scalars()
        .all()
    )
    # Also include older txns that were refunded in either window (created_at
    # may predate prev_since while refunded_at falls inside).
    refund_extra = (
        (
            await session.execute(
                select(Transaction).where(
                    Transaction.refunded_at.is_not(None),
                    Transaction.refunded_at >= prev_since,
                    Transaction.created_at < prev_since,
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {t.id: t for t in transactions}
    for t in refund_extra:
        by_id.setdefault(t.id, t)
    transactions = list(by_id.values())

    payouts = (
        (
            await session.execute(
                select(PayoutRequest).where(
                    PayoutRequest.status == PayoutStatus.completed,
                    PayoutRequest.processed_at >= prev_since,
                )
            )
        )
        .scalars()
        .all()
    )

    gross_revenue_usd, refunds_usd, net_revenue_usd, advisor_payout_usd = _finance_window_totals(
        transactions, payouts, since, now
    )
    prev_gross, prev_refunds, prev_net, prev_payout = _finance_window_totals(
        transactions, payouts, prev_since, since
    )

    # Trends: current window only.
    current_gross = [
        t
        for t in transactions
        if t.status in _GROSS_STATUSES and _as_utc(t.created_at) >= since
    ]
    revenue_trend_map: dict[str, float] = defaultdict(float)
    for t in current_gross:
        revenue_trend_map[_month_key(t.created_at)] += t.amount_usd
    revenue_trend = [
        MonthlyAmountPoint(month=month, amount_usd=round(amount, 2))
        for month, amount in sorted(revenue_trend_map.items())
    ]

    refund_trend_map: dict[str, float] = defaultdict(float)
    for t in transactions:
        if t.refunded_at is None:
            continue
        refunded_at = _as_utc(t.refunded_at)
        if refunded_at < since:
            continue
        refund_trend_map[_month_key(refunded_at)] += t.refunded_amount_usd or 0.0
    refund_trend = [
        MonthlyAmountPoint(month=month, amount_usd=round(amount, 2))
        for month, amount in sorted(refund_trend_map.items())
    ]

    payout_trend_map: dict[str, float] = defaultdict(float)
    for p in payouts:
        assert p.processed_at is not None
        if _as_utc(p.processed_at) < since:
            continue
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
        gross_revenue_change_pct=_change_pct(gross_revenue_usd, prev_gross),
        net_revenue_change_pct=_change_pct(net_revenue_usd, prev_net),
        refunds_change_pct=_change_pct(refunds_usd, prev_refunds),
        advisor_payout_change_pct=_change_pct(advisor_payout_usd, prev_payout),
        revenue_trend=revenue_trend,
        refund_trend=refund_trend,
        monthly_payouts=monthly_payouts,
    )


# ── AI Analytics ─────────────────────────────────────────────────────────────


def _ai_visa_counts(assessments: Sequence[Assessment]) -> dict[VisaType, int]:
    counts: dict[VisaType, int] = {vt: 0 for vt in _AI_VISA_ORDER}
    for a in assessments:
        parsed = parse_visa_type(a.visa_type)
        if parsed is not None:
            counts[parsed] = counts.get(parsed, 0) + 1
    return counts


def _ai_assessment_distribution(
    current: Sequence[Assessment], previous: Sequence[Assessment]
) -> list[AssessmentDistributionPoint]:
    cur_counts = _ai_visa_counts(current)
    prev_counts = _ai_visa_counts(previous)
    return [
        AssessmentDistributionPoint(
            key=vt.value,
            label=VISA_TYPE_LABELS[vt],
            value=cur_counts[vt],
            change_pct=_change_pct(cur_counts[vt], prev_counts[vt]),
        )
        for vt in _AI_VISA_ORDER
    ]


def _ai_eligibility_breakdown(
    completed: Sequence[Assessment],
) -> list[EligibilityBreakdownPoint]:
    by_visa: dict[VisaType, list[Assessment]] = {vt: [] for vt in _AI_VISA_ORDER}
    for a in completed:
        parsed = parse_visa_type(a.visa_type)
        if parsed is None or a.tier is None:
            continue
        by_visa.setdefault(parsed, []).append(a)

    rows: list[EligibilityBreakdownPoint] = []
    for vt in _AI_VISA_ORDER:
        group = by_visa[vt]
        total = len(group)
        if total == 0:
            rows.append(
                EligibilityBreakdownPoint(
                    category=VISA_TYPE_LABELS[vt], low=0.0, medium=0.0, high=0.0
                )
            )
            continue
        high = sum(1 for a in group if a.tier in _ELIGIBILITY_HIGH)
        medium = sum(1 for a in group if a.tier in _ELIGIBILITY_MEDIUM)
        low = sum(1 for a in group if a.tier in _ELIGIBILITY_LOW)
        rows.append(
            EligibilityBreakdownPoint(
                category=VISA_TYPE_LABELS[vt],
                low=round(100.0 * low / total, 1),
                medium=round(100.0 * medium / total, 1),
                high=round(100.0 * high / total, 1),
            )
        )
    return rows


def _ai_funnel_values(
    assessments: Sequence[Assessment],
    leads: Sequence[AdvisorLead],
    bookings: Sequence[Booking],
) -> dict[str, int]:
    """Seeker match conversion proxies (no dedicated impression/click events).

    impressions     — assessments started
    matches_shown   — assessments completed (results + match list shown)
    advisors_clicked — distinct seeker→advisor bookings after a completed assessment
    session_booked  — those bookings that reached confirmed/completed
    """
    completed_ids = {a.id for a in assessments if a.status == AssessmentStatus.completed}
    completed_seekers = {
        a.user_id for a in assessments if a.status == AssessmentStatus.completed
    }
    # Prefer lead-backed matches when present; fall back to completed count.
    matched_assessments = {lead.assessment_id for lead in leads} & completed_ids
    matches_shown = len(matched_assessments) if matched_assessments else len(completed_ids)

    post_match_bookings = [
        b for b in bookings if b.seeker_id in completed_seekers
    ]
    advisors_clicked = len({(b.seeker_id, b.advisor_id) for b in post_match_bookings})
    session_booked = sum(
        1 for b in post_match_bookings if b.status in _SESSION_BOOKED_STATUSES
    )
    return {
        "impressions": len(assessments),
        "matches_shown": matches_shown,
        "advisors_clicked": advisors_clicked,
        "session_booked": session_booked,
    }


def _ai_advisor_match_funnel(
    cur_assessments: Sequence[Assessment],
    prev_assessments: Sequence[Assessment],
    cur_leads: Sequence[AdvisorLead],
    prev_leads: Sequence[AdvisorLead],
    cur_bookings: Sequence[Booking],
    prev_bookings: Sequence[Booking],
) -> list[AdvisorMatchFunnelPoint]:
    cur = _ai_funnel_values(cur_assessments, cur_leads, cur_bookings)
    prev = _ai_funnel_values(prev_assessments, prev_leads, prev_bookings)
    return [
        AdvisorMatchFunnelPoint(
            key=key,
            label=label,
            value=cur[key],
            change_pct=_change_pct(cur[key], prev[key]),
        )
        for key, label in _FUNNEL_STAGES
    ]


async def get_ai_analytics(session: AsyncSession, days: int = 270) -> AIAnalyticsRead:
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    prev_since = since - timedelta(days=days)

    assessments = list(
        (
            await session.execute(
                select(Assessment).where(Assessment.created_at >= prev_since)
            )
        )
        .scalars()
        .all()
    )
    cur_assessments = [a for a in assessments if _as_utc(a.created_at) >= since]
    prev_assessments = [
        a for a in assessments if prev_since <= _as_utc(a.created_at) < since
    ]

    leads = list(
        (
            await session.execute(
                select(AdvisorLead).where(AdvisorLead.created_at >= prev_since)
            )
        )
        .scalars()
        .all()
    )
    cur_leads = [lead for lead in leads if _as_utc(lead.created_at) >= since]
    prev_leads = [
        lead for lead in leads if prev_since <= _as_utc(lead.created_at) < since
    ]

    bookings = list(
        (
            await session.execute(select(Booking).where(Booking.created_at >= prev_since))
        )
        .scalars()
        .all()
    )
    cur_bookings = [b for b in bookings if _as_utc(b.created_at) >= since]
    prev_bookings = [b for b in bookings if prev_since <= _as_utc(b.created_at) < since]

    completed = [a for a in cur_assessments if a.status == AssessmentStatus.completed]

    return AIAnalyticsRead(
        window_days=days,
        assessment_distribution=_ai_assessment_distribution(
            cur_assessments, prev_assessments
        ),
        advisor_match_funnel=_ai_advisor_match_funnel(
            cur_assessments,
            prev_assessments,
            cur_leads,
            prev_leads,
            cur_bookings,
            prev_bookings,
        ),
        eligibility_breakdown=_ai_eligibility_breakdown(completed),
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
