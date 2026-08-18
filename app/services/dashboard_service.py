"""Admin dashboard home screen — stat cards, trend/volume/breakdown charts,
and the live-merged recent-activities feed. Distinct from analytics_service.py
(the per-tab Analytics deep-dive) — this is the single home-screen summary.

Aggregations stay in SQL (GROUP BY / conditional COUNT / UNION). Python only
zero-fills chart buckets and maps the already-limited activity rows.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, case, cast, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import CompoundSelect, Select

from app.api.pagination import PaginationParams
from app.models.advisor_credential import AdvisorCredential
from app.models.assessment import Assessment
from app.models.booking import Booking, BookingStatus
from app.models.review import ModerationStatus, Review
from app.models.seeker_document import SeekerDocument
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.analytics import MonthlyCountPoint
from app.schemas.dashboard import (
    ActivityEventType,
    ActivityFeedItemRead,
    DashboardSummaryRead,
    RevenueBreakdownSliceRead,
)

_GROSS_STATUSES = (
    TransactionStatus.succeeded,
    TransactionStatus.partially_refunded,
    TransactionStatus.refunded,
)

_HOME_ACTIVITY_LIMIT = 6

_EMPTY = literal("", type_=String)


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _shift_month(dt: datetime, months: int) -> datetime:
    month_index = dt.year * 12 + dt.month - 1 + months
    return datetime(month_index // 12, month_index % 12 + 1, 1, tzinfo=UTC)


def _dashboard_since(days: int | None) -> datetime | None:
    """Resolve the window start; ``None`` = all-time.

    Month-based filters (90 / 180 / 365) use complete calendar-month windows.
    """
    if days is None:
        return None
    now = datetime.now(UTC)
    calendar_months = {90: 3, 180: 6, 365: 12}.get(days)
    if calendar_months is None:
        return now - timedelta(days=days)
    return _shift_month(now, -(calendar_months - 1))


def _as_text(expr: Any) -> Any:
    return func.coalesce(cast(expr, String), _EMPTY)


def _event_literal(event_type: ActivityEventType) -> Any:
    return literal(event_type.value, type_=String)


def _bucket_key(parts: Sequence[Any], *, daily: bool) -> str:
    year, month = int(parts[0]), int(parts[1])
    if daily:
        return f"{year:04d}-{month:02d}-{int(parts[2]):02d}"
    return f"{year:04d}-{month:02d}"


def _period_parts(column: Any, *, daily: bool) -> tuple[Any, ...]:
    year = func.extract("year", column)
    month = func.extract("month", column)
    if daily:
        return year, month, func.extract("day", column)
    return year, month


def _monthly_points_from_counts(
    counts: dict[str, int], since: datetime | None, *, all_time: bool
) -> list[MonthlyCountPoint]:
    if all_time and not counts:
        return []

    if all_time or since is None:
        year, month = (int(p) for p in min(counts).split("-")[:2])
        start = datetime(year, month, 1, tzinfo=UTC)
    else:
        start = since
    cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    end = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    points: list[MonthlyCountPoint] = []
    while cursor <= end:
        key = _month_key(cursor)
        points.append(MonthlyCountPoint(month=key, count=counts.get(key, 0)))
        cursor = _shift_month(cursor, 1)
    return points


def _daily_points_from_counts(counts: dict[str, int], since: datetime) -> list[MonthlyCountPoint]:
    """One UTC calendar day per point (``month`` = ``YYYY-MM-DD``), zero-filled."""
    today = datetime.now(UTC).date()
    cursor = since.date()
    points: list[MonthlyCountPoint] = []
    while cursor <= today:
        key = cursor.strftime("%Y-%m-%d")
        points.append(MonthlyCountPoint(month=key, count=counts.get(key, 0)))
        cursor += timedelta(days=1)
    return points


def _trend_from_counts(
    counts: dict[str, int], since: datetime | None, days: int | None
) -> list[MonthlyCountPoint]:
    if days == 7 and since is not None:
        return _daily_points_from_counts(counts, since)
    return _monthly_points_from_counts(counts, since, all_time=days is None)


def _bucket_service_type(service_type: str) -> str:
    """Case-insensitive substring match, checked in this order (a value could
    contain both — "review" wins since document-review is more specific):
      contains "review"  -> "Document Review"
      contains "consult" -> "Advisor"
      otherwise           -> "Platform"
    None of today's literal service_type values collide, but this ordering
    is deliberate for future values like "consultation_with_review".
    """
    s = service_type.lower()
    if "review" in s:
        return "Document Review"
    if "consult" in s:
        return "Advisor"
    return "Platform"


# ── Dashboard summary ────────────────────────────────────────────────────────


async def get_dashboard_summary(
    session: AsyncSession, days: int | None = None
) -> DashboardSummaryRead:
    since = _dashboard_since(days)
    stats = await _user_stat_counts(session, since)
    revenue_today_usd = await _revenue_today_usd(session)
    return DashboardSummaryRead(
        window_days=days,
        total_users=stats["total_users"],
        total_seekers=stats["total_seekers"],
        verified_seekers=stats["verified_seekers"],
        total_advisors=stats["total_advisors"],
        verified_advisors=stats["verified_advisors"],
        active_advisors=stats["active_advisors"],
        revenue_today_usd=revenue_today_usd,
        user_registration_trend=await _user_registration_trend(session, since, days),
        ai_assessment_volume=await _ai_assessment_volume(session, since, days),
        revenue_breakdown=await _revenue_breakdown(session, since),
        recent_activities=await get_recent_activities(session, days, limit=_HOME_ACTIVITY_LIMIT),
    )


async def _user_stat_counts(session: AsyncSession, since: datetime | None) -> dict[str, int]:
    """One users-table scan: total + role/verification slices."""
    seeker = User.role == UserRole.seeker
    advisor = User.role == UserRole.advisor
    approved = User.verification_status == VerificationStatus.approved
    stmt = select(
        func.count().label("total_users"),
        func.coalesce(func.sum(case((seeker, 1), else_=0)), 0).label("total_seekers"),
        func.coalesce(
            func.sum(case((seeker & User.email_verified_at.is_not(None), 1), else_=0)),
            0,
        ).label("verified_seekers"),
        func.coalesce(func.sum(case((advisor, 1), else_=0)), 0).label("total_advisors"),
        func.coalesce(
            func.sum(case((advisor & approved, 1), else_=0)),
            0,
        ).label("verified_advisors"),
        func.coalesce(
            func.sum(case((advisor & User.is_active.is_(True) & approved, 1), else_=0)),
            0,
        ).label("active_advisors"),
    ).select_from(User)
    if since is not None:
        stmt = stmt.where(User.created_at >= since)
    row = (await session.execute(stmt)).one()
    return {
        "total_users": int(row.total_users),
        "total_seekers": int(row.total_seekers),
        "verified_seekers": int(row.verified_seekers),
        "total_advisors": int(row.total_advisors),
        "verified_advisors": int(row.verified_advisors),
        "active_advisors": int(row.active_advisors),
    }


async def _revenue_today_usd(session: AsyncSession) -> float:
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
    return round(float(revenue_today_usd), 2)


async def _grouped_timestamp_counts(
    session: AsyncSession,
    column: Any,
    since: datetime | None,
    *,
    daily: bool,
) -> dict[str, int]:
    parts = _period_parts(column, daily=daily)
    stmt = select(*parts, func.count())
    if since is not None:
        stmt = stmt.where(column >= since)
    stmt = stmt.group_by(*parts)
    counts: dict[str, int] = {}
    for *bucket, count in (await session.execute(stmt)).all():
        counts[_bucket_key(tuple(bucket), daily=daily)] = int(count)
    return counts


async def _user_registration_trend(
    session: AsyncSession, since: datetime | None, days: int | None
) -> list[MonthlyCountPoint]:
    """ALL users regardless of role (seeker+advisor combined) — the mockup
    shows one line with no role split."""
    counts = await _grouped_timestamp_counts(
        session, User.created_at, since, daily=days == 7 and since is not None
    )
    return _trend_from_counts(counts, since, days)


async def _ai_assessment_volume(
    session: AsyncSession, since: datetime | None, days: int | None
) -> list[MonthlyCountPoint]:
    """All Assessment rows regardless of status — matches assessment_service
    .get_analytics()'s own definition of "volume" as assessments *started*,
    not just completed."""
    counts = await _grouped_timestamp_counts(
        session, Assessment.created_at, since, daily=days == 7 and since is not None
    )
    return _trend_from_counts(counts, since, days)


async def _revenue_breakdown(
    session: AsyncSession, since: datetime | None
) -> list[RevenueBreakdownSliceRead]:
    stmt = (
        select(Booking.service_type, func.coalesce(func.sum(Transaction.amount_usd), 0.0))
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(Transaction.status.in_(_GROSS_STATUSES))
        .group_by(Booking.service_type)
    )
    if since is not None:
        stmt = stmt.where(Transaction.created_at >= since)
    totals: dict[str, float] = defaultdict(float)
    for service_type, amount_usd in (await session.execute(stmt)).all():
        totals[_bucket_service_type(service_type)] += float(amount_usd)
    grand_total = sum(totals.values())
    if grand_total <= 0:
        return []
    return [
        RevenueBreakdownSliceRead(
            label=label,
            amount_usd=round(amount, 2),
            pct=round(100.0 * amount / grand_total, 2),
        )
        for label, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        if amount > 0  # omit empty buckets — no 0% wedge, matches how a real donut renders
    ]


# ── Recent activities — UNION in SQL, then page/limit ─────────────────────────


def _with_since(stmt: Select[Any], column: Any, since: datetime | None) -> Select[Any]:
    if since is not None:
        return stmt.where(column >= since)
    return stmt


def _activity_union_stmt(since: datetime | None) -> CompoundSelect[Any]:
    """One row per feed event: occurred_at, event_type, col_a, col_b.

    Description text is assembled in Python from those two payload columns so
    the UNION stays dialect-portable (SQLite tests + Postgres).
    """
    seeker, advisor = aliased(User), aliased(User)

    new_users = _with_since(
        select(
            User.created_at.label("occurred_at"),
            _event_literal(ActivityEventType.new_user_registered).label("event_type"),
            _as_text(User.full_name).label("col_a"),
            _as_text(SeekerProfile.country_of_residence).label("col_b"),
        )
        .outerjoin(SeekerProfile, SeekerProfile.user_id == User.id)
        .where(User.role == UserRole.seeker),
        User.created_at,
        since,
    )
    advisor_apps = _with_since(
        select(
            User.created_at.label("occurred_at"),
            _event_literal(ActivityEventType.advisor_application_submitted).label("event_type"),
            _as_text(User.full_name).label("col_a"),
            _EMPTY.label("col_b"),
        ).where(User.role == UserRole.advisor),
        User.created_at,
        since,
    )
    sessions = _with_since(
        select(
            Booking.updated_at.label("occurred_at"),
            _event_literal(ActivityEventType.session_completed).label("event_type"),
            _as_text(seeker.full_name).label("col_a"),
            _as_text(advisor.full_name).label("col_b"),
        )
        .join(seeker, seeker.id == Booking.seeker_id)
        .join(advisor, advisor.id == Booking.advisor_id)
        .where(Booking.status == BookingStatus.completed),
        Booking.updated_at,
        since,
    )
    refunds = _with_since(
        select(
            Transaction.refunded_at.label("occurred_at"),
            _event_literal(ActivityEventType.refund_request).label("event_type"),
            _as_text(Transaction.invoice_number).label("col_a"),
            _as_text(Transaction.id).label("col_b"),
        ).where(Transaction.refunded_at.is_not(None)),
        Transaction.refunded_at,
        since,
    )
    flagged = _with_since(
        select(
            Review.updated_at.label("occurred_at"),
            _event_literal(ActivityEventType.review_flagged).label("event_type"),
            _as_text(Review.id).label("col_a"),
            _EMPTY.label("col_b"),
        ).where(Review.moderation_status == ModerationStatus.flagged),
        Review.updated_at,
        since,
    )
    seeker_docs = _with_since(
        select(
            SeekerDocument.created_at.label("occurred_at"),
            _event_literal(ActivityEventType.document_uploaded).label("event_type"),
            _as_text(User.full_name).label("col_a"),
            literal("seeker", type_=String).label("col_b"),
        ).join(User, User.id == SeekerDocument.seeker_id),
        SeekerDocument.created_at,
        since,
    )
    credentials = _with_since(
        select(
            AdvisorCredential.created_at.label("occurred_at"),
            _event_literal(ActivityEventType.document_uploaded).label("event_type"),
            _as_text(User.full_name).label("col_a"),
            literal("advisor", type_=String).label("col_b"),
        ).join(User, User.id == AdvisorCredential.user_id),
        AdvisorCredential.created_at,
        since,
    )
    return union_all(new_users, advisor_apps, sessions, refunds, flagged, seeker_docs, credentials)


def _activity_from_row(
    occurred_at: datetime, event_type: str, col_a: str | None, col_b: str | None
) -> ActivityFeedItemRead:
    a = (col_a or "").strip()
    b = (col_b or "").strip()
    kind = ActivityEventType(event_type)
    if kind is ActivityEventType.new_user_registered:
        name = a or "A user"
        suffix = f" ({b})" if b else ""
        title, description = "New User Register", f"{name}{suffix} just signed up"
    elif kind is ActivityEventType.advisor_application_submitted:
        title, description = "Advisor Application Submitted", f"{a or 'An advisor'} applied"
    elif kind is ActivityEventType.session_completed:
        title = "Session Completed"
        description = f"Between {a or 'a seeker'} and {b or 'an advisor'}"
    elif kind is ActivityEventType.refund_request:
        title = "Refund Request"
        description = f"Order #{a}" if a else f"Order #{b[:8]}"
    elif kind is ActivityEventType.review_flagged:
        title, description = "New Review Flagged", f"Review #{a[:8]} flagged for moderation"
    else:
        who = a or ("An advisor" if b == "advisor" else "A seeker")
        what = "a credential" if b == "advisor" else "a document"
        title, description = "Document Uploaded", f"{who} uploaded {what}"
    return ActivityFeedItemRead(
        event_type=kind,
        occurred_at=occurred_at,
        title=title,
        description=description,
    )


async def _activity_rows(
    session: AsyncSession, since: datetime | None, *, offset: int, limit: int
) -> list[ActivityFeedItemRead]:
    src = _activity_union_stmt(since).subquery("activities")
    stmt = (
        select(src.c.occurred_at, src.c.event_type, src.c.col_a, src.c.col_b)
        .order_by(src.c.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return [_activity_from_row(*row) for row in (await session.execute(stmt)).all()]


async def _activity_total(session: AsyncSession, since: datetime | None) -> int:
    src = _activity_union_stmt(since).subquery("activities")
    return int((await session.execute(select(func.count()).select_from(src))).scalar_one())


async def get_recent_activities(
    session: AsyncSession, days: int | None = None, limit: int = _HOME_ACTIVITY_LIMIT
) -> list[ActivityFeedItemRead]:
    since = _dashboard_since(days)
    return await _activity_rows(session, since, offset=0, limit=limit)


async def list_recent_activities_page(
    session: AsyncSession, days: int | None, params: PaginationParams
) -> tuple[list[ActivityFeedItemRead], int]:
    """Paginate the merged feed in SQL (UNION ALL + LIMIT/OFFSET).

    paginate() (app/api/pagination.py) operates on a single table Select;
    this feed is a union of 7 sources. Response envelope (Meta.pagination)
    stays identical to every other paginated admin list.
    """
    since = _dashboard_since(days)
    total = await _activity_total(session, since)
    items = await _activity_rows(session, since, offset=params.offset, limit=params.limit)
    return items, total
