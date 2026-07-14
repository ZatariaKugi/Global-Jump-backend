"""Admin dashboard home screen — stat cards, trend/volume/breakdown charts,
and the live-merged recent-activities feed. Distinct from analytics_service.py
(the per-tab Analytics deep-dive) — this is the single home-screen summary.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _bucket_service_type(service_type: str) -> str:
    """Case-insensitive substring match, checked in this order (a value could
    contain both — "review" wins since document-review is more specific):
      contains "review"  -> "Document Review"
      contains "consult" -> "Consultant"
      otherwise           -> "Others"
    None of today's literal service_type values collide, but this ordering
    is deliberate for future values like "consultation_with_review".
    """
    s = service_type.lower()
    if "review" in s:
        return "Document Review"
    if "consult" in s:
        return "Consultant"
    return "Others"


# ── Dashboard summary ────────────────────────────────────────────────────────


async def get_dashboard_summary(session: AsyncSession, days: int = 180) -> DashboardSummaryRead:
    since = _since(days)

    total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    total_advisors = (
        await session.execute(
            select(func.count()).select_from(User).where(User.role == UserRole.advisor)
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

    return DashboardSummaryRead(
        window_days=days,
        total_users=total_users,
        total_advisors=total_advisors,
        active_advisors=active_advisors,
        revenue_today_usd=round(revenue_today_usd, 2),
        user_registration_trend=await _user_registration_trend(session, since),
        ai_assessment_volume=await _ai_assessment_volume(session, since),
        revenue_breakdown=await _revenue_breakdown(session, since),
        recent_activities=await get_recent_activities(session, days, limit=6),
    )


async def _user_registration_trend(
    session: AsyncSession, since: datetime
) -> list[MonthlyCountPoint]:
    """ALL users regardless of role (seeker+advisor combined) — the mockup
    shows one line with no role split."""
    rows = (
        (await session.execute(select(User.created_at).where(User.created_at >= since)))
        .scalars()
        .all()
    )
    counts: dict[str, int] = defaultdict(int)
    for created_at in rows:
        counts[_month_key(created_at)] += 1
    return [MonthlyCountPoint(month=m, count=c) for m, c in sorted(counts.items())]


async def _ai_assessment_volume(session: AsyncSession, since: datetime) -> list[MonthlyCountPoint]:
    """All Assessment rows regardless of status — matches assessment_service
    .get_analytics()'s own definition of "volume" as assessments *started*,
    not just completed."""
    rows = (
        (await session.execute(select(Assessment.created_at).where(Assessment.created_at >= since)))
        .scalars()
        .all()
    )
    counts: dict[str, int] = defaultdict(int)
    for created_at in rows:
        counts[_month_key(created_at)] += 1
    return [MonthlyCountPoint(month=m, count=c) for m, c in sorted(counts.items())]


async def _revenue_breakdown(
    session: AsyncSession, since: datetime
) -> list[RevenueBreakdownSliceRead]:
    rows = (
        await session.execute(
            select(Transaction.amount_usd, Booking.service_type)
            .join(Booking, Booking.id == Transaction.booking_id)
            .where(Transaction.status.in_(_GROSS_STATUSES), Transaction.created_at >= since)
        )
    ).all()
    totals: dict[str, float] = defaultdict(float)
    for amount_usd, service_type in rows:
        totals[_bucket_service_type(service_type)] += amount_usd
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


# ── Recent activities — merged live query ────────────────────────────────────


async def _new_user_events(session: AsyncSession, since: datetime) -> list[ActivityFeedItemRead]:
    rows = (
        await session.execute(
            select(User.created_at, User.full_name, SeekerProfile.country_of_residence)
            .outerjoin(SeekerProfile, SeekerProfile.user_id == User.id)
            .where(User.role == UserRole.seeker, User.created_at >= since)
        )
    ).all()
    items = []
    for created_at, full_name, country in rows:
        name = full_name or "A user"
        suffix = f" ({country})" if country else ""
        items.append(
            ActivityFeedItemRead(
                event_type=ActivityEventType.new_user_registered,
                occurred_at=created_at,
                title="New User Register",
                description=f"{name}{suffix} just signed up",
            )
        )
    return items


async def _advisor_application_events(
    session: AsyncSession, since: datetime
) -> list[ActivityFeedItemRead]:
    rows = (
        await session.execute(
            select(User.created_at, User.full_name).where(
                User.role == UserRole.advisor, User.created_at >= since
            )
        )
    ).all()
    return [
        ActivityFeedItemRead(
            event_type=ActivityEventType.advisor_application_submitted,
            occurred_at=created_at,
            title="Advisor Application Submitted",
            description=f"{full_name or 'An advisor'} applied",
        )
        for created_at, full_name in rows
    ]


async def _session_completed_events(
    session: AsyncSession, since: datetime
) -> list[ActivityFeedItemRead]:
    seeker, advisor = aliased(User), aliased(User)
    rows = (
        await session.execute(
            select(Booking.updated_at, seeker.full_name, advisor.full_name)
            .join(seeker, seeker.id == Booking.seeker_id)
            .join(advisor, advisor.id == Booking.advisor_id)
            .where(Booking.status == BookingStatus.completed, Booking.updated_at >= since)
        )
    ).all()
    return [
        ActivityFeedItemRead(
            event_type=ActivityEventType.session_completed,
            occurred_at=updated_at,
            title="Session Completed",
            description=f"Between {seeker_name or 'a seeker'} and {advisor_name or 'an advisor'}",
        )
        for updated_at, seeker_name, advisor_name in rows
    ]


async def _refund_request_events(
    session: AsyncSession, since: datetime
) -> list[ActivityFeedItemRead]:
    rows = (
        await session.execute(
            select(Transaction.refunded_at, Transaction.invoice_number, Transaction.id).where(
                Transaction.refunded_at.is_not(None), Transaction.refunded_at >= since
            )
        )
    ).all()
    return [
        ActivityFeedItemRead(
            event_type=ActivityEventType.refund_request,
            occurred_at=refunded_at,
            title="Refund Request",
            description=(
                f"Order #{invoice_number}"
                if invoice_number is not None
                else f"Order #{str(txn_id)[:8]}"
            ),
        )
        for refunded_at, invoice_number, txn_id in rows
    ]


async def _review_flagged_events(
    session: AsyncSession, since: datetime
) -> list[ActivityFeedItemRead]:
    rows = (
        await session.execute(
            select(Review.updated_at, Review.id).where(
                Review.moderation_status == ModerationStatus.flagged, Review.updated_at >= since
            )
        )
    ).all()
    return [
        ActivityFeedItemRead(
            event_type=ActivityEventType.review_flagged,
            occurred_at=updated_at,
            title="New Review Flagged",
            description=f"Review #{str(review_id)[:8]} flagged for moderation",
        )
        for updated_at, review_id in rows
    ]


async def _document_uploaded_events(
    session: AsyncSession, since: datetime
) -> list[ActivityFeedItemRead]:
    """Merges two source tables (seeker documents + advisor credentials) into
    one event type, distinguished only in the description text."""
    seeker_rows = (
        await session.execute(
            select(SeekerDocument.created_at, User.full_name)
            .join(User, User.id == SeekerDocument.seeker_id)
            .where(SeekerDocument.created_at >= since)
        )
    ).all()
    credential_rows = (
        await session.execute(
            select(AdvisorCredential.created_at, User.full_name)
            .join(User, User.id == AdvisorCredential.user_id)
            .where(AdvisorCredential.created_at >= since)
        )
    ).all()
    items = [
        ActivityFeedItemRead(
            event_type=ActivityEventType.document_uploaded,
            occurred_at=created_at,
            title="Document Uploaded",
            description=f"{full_name or 'A seeker'} uploaded a document",
        )
        for created_at, full_name in seeker_rows
    ]
    items += [
        ActivityFeedItemRead(
            event_type=ActivityEventType.document_uploaded,
            occurred_at=created_at,
            title="Document Uploaded",
            description=f"{full_name or 'An advisor'} uploaded a credential",
        )
        for created_at, full_name in credential_rows
    ]
    return items


async def _merged_activities(
    session: AsyncSession, days: int
) -> tuple[list[ActivityFeedItemRead], int]:
    since = _since(days)
    all_items: list[ActivityFeedItemRead] = []
    for fetch in (
        _new_user_events,
        _advisor_application_events,
        _session_completed_events,
        _refund_request_events,
        _review_flagged_events,
        _document_uploaded_events,
    ):
        all_items.extend(await fetch(session, since))
    all_items.sort(key=lambda item: item.occurred_at, reverse=True)
    return all_items, len(all_items)


async def get_recent_activities(
    session: AsyncSession, days: int = 180, limit: int = 6
) -> list[ActivityFeedItemRead]:
    items, _total = await _merged_activities(session, days)
    return items[:limit]


async def list_recent_activities_page(
    session: AsyncSession, days: int, params: PaginationParams
) -> tuple[list[ActivityFeedItemRead], int]:
    """Manual list-pagination — paginate() (app/api/pagination.py) operates on
    a single SQL Select; this feed is merged in Python across 6 source
    queries, so there's no one statement to paginate. Keeps the response
    envelope (Meta.pagination) identical to every other paginated admin list
    endpoint — only the server-side slicing mechanism differs."""
    all_items, total = await _merged_activities(session, days)
    return all_items[params.offset : params.offset + params.limit], total
