"""Advisor dashboard aggregate (FE ``/advisor/dashboard``).

One orchestration layer over existing services/tables — it owns no new state
except the regulatory feed. The ``days`` window (7/30/90) scopes the stat tiles;
profile completion is a point-in-time figure and is not windowed.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.advisor_lead import AdvisorLead, AdvisorLeadStatus
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking
from app.models.regulatory_update import RegulatoryUpdate
from app.models.review import Review
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.advisor_dashboard import (
    AdvisorDashboardRead,
    ClientInquiryRead,
    ClientInquiryStatus,
    DashboardStats,
    NextUpcomingRead,
    RegulatoryUpdateRead,
)
from app.services import (
    booking_service,
    conversation_service,
)
from app.services.review_service import PUBLIC_STATUSES as REVIEW_PUBLIC_STATUSES

# How many rows the summary cards show before "See all".
_REGULATORY_CARD_LIMIT = 5
_INQUIRIES_CARD_LIMIT = 5

# Profile-completion checklist: each item is (weight, satisfied?). Weights are
# relative — the percent is (satisfied weight / total weight) rounded to int.
# Chosen so a freshly-onboarded advisor lands in the ~70–90% band and the tile
# nudges them toward the missing pieces (photo, languages, bookable services).


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def profile_completion_percent(profile: AdvisorProfile) -> int:
    """Weighted 0–100 completeness score over the editable profile surface."""
    checks: list[tuple[int, bool]] = [
        (2, bool(profile.bio and profile.bio.strip())),
        (1, profile.years_of_experience is not None),
        (1, bool(profile.country_of_residence)),
        (2, bool(profile.expertise_description and profile.expertise_description.strip())),
        (2, bool(profile.visa_specializations)),
        (2, bool(profile.country_expertise)),
        (1, bool(profile.languages)),
        (1, bool(profile.offered_services)),
        (1, bool(profile.profile_photo_url)),
        (2, bool(profile.services)),  # at least one bookable service (duration + price)
    ]
    total = sum(weight for weight, _ in checks)
    got = sum(weight for weight, ok in checks if ok)
    return round(100 * got / total) if total else 0


async def _new_leads_count(session: AsyncSession, advisor_id: uuid.UUID, since: datetime) -> int:
    result = await session.execute(
        select(func.count(AdvisorLead.id)).where(
            AdvisorLead.advisor_id == advisor_id,
            AdvisorLead.status == AdvisorLeadStatus.new,
            AdvisorLead.created_at >= since,
        )
    )
    return int(result.scalar_one())


async def _total_earned_usd(session: AsyncSession, advisor_id: uuid.UUID, since: datetime) -> float:
    """Advisor payout on succeeded transactions in-window (matches earnings math)."""
    result = await session.execute(
        select(func.coalesce(func.sum(Transaction.advisor_payout_usd), 0.0))
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(
            Booking.advisor_id == advisor_id,
            Transaction.status == TransactionStatus.succeeded,
            Transaction.is_archived.is_(False),
            Transaction.created_at >= since,
        )
    )
    return round(float(result.scalar_one()), 2)


async def _pending_reviews_count(session: AsyncSession, advisor_id: uuid.UUID) -> int:
    """Public reviews awaiting an advisor reply — all-time, not windowed."""
    result = await session.execute(
        select(func.count(Review.id)).where(
            Review.advisor_id == advisor_id,
            Review.advisor_response.is_(None),
            Review.moderation_status.in_(REVIEW_PUBLIC_STATUSES),
        )
    )
    return int(result.scalar_one())


async def _next_upcoming(session: AsyncSession, advisor_id: uuid.UUID) -> NextUpcomingRead | None:
    booking = await booking_service.get_next_upcoming(session, advisor_id, UserRole.advisor)
    if booking is None:
        return None
    seeker = await session.get(User, booking.seeker_id)
    return NextUpcomingRead(
        booking_id=booking.id,
        appointment_id=booking_service.appointment_id_str(booking),
        seeker_id=booking.seeker_id,
        seeker_name=seeker.full_name if seeker else None,
        service_type=booking.service_type,
        status=booking.status.value,
        scheduled_start=booking.scheduled_start,
        scheduled_end=booking.scheduled_end,
    )


async def _last_message_sender(
    session: AsyncSession, conversation_id: uuid.UUID
) -> uuid.UUID | None:
    last = await conversation_service.last_message(session, conversation_id)
    return last.sender_id if last else None


def regulatory_list_stmt() -> Select[tuple[RegulatoryUpdate]]:
    return (
        select(RegulatoryUpdate)
        .where(RegulatoryUpdate.is_archived.is_(False))
        .order_by(RegulatoryUpdate.published_at.desc())
    )


async def _regulatory_updates(
    session: AsyncSession, since: datetime, limit: int
) -> list[RegulatoryUpdateRead]:
    stmt = regulatory_list_stmt().where(RegulatoryUpdate.published_at >= since).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [RegulatoryUpdateRead.model_validate(r) for r in rows]


def _inquiry_status(
    unread: int, last_sender_id: uuid.UUID | None, advisor_id: uuid.UUID
) -> ClientInquiryStatus:
    if last_sender_id is None:
        return "new"
    if last_sender_id == advisor_id:
        return "responded"
    return "unread" if unread > 0 else "responded"


async def _client_inquiries(
    session: AsyncSession, advisor: User, settings: Settings, limit: int
) -> list[ClientInquiryRead]:
    stmt = conversation_service.list_for_user_stmt(advisor.id).limit(limit)
    conversations = (await session.execute(stmt)).scalars().all()
    inquiries: list[ClientInquiryRead] = []
    for conversation in conversations:
        base = await conversation_service.build_conversation_read(
            session, conversation, advisor.id, settings
        )
        last_sender_id = await _last_message_sender(session, conversation.id)
        status = _inquiry_status(base.unread_count, last_sender_id, advisor.id)
        inquiries.append(ClientInquiryRead(**base.model_dump(), status=status))
    return inquiries


async def get_dashboard(
    session: AsyncSession,
    advisor: User,
    profile: AdvisorProfile,
    settings: Settings,
    days: int,
) -> AdvisorDashboardRead:
    since = _since(days)
    stats = DashboardStats(
        new_leads_count=await _new_leads_count(session, advisor.id, since),
        total_earned_usd=await _total_earned_usd(session, advisor.id, since),
        pending_reviews_count=await _pending_reviews_count(session, advisor.id),
        profile_completion_percent=profile_completion_percent(profile),
    )
    return AdvisorDashboardRead(
        window_days=days,
        next_upcoming=await _next_upcoming(session, advisor.id),
        stats=stats,
        regulatory_updates=await _regulatory_updates(session, since, _REGULATORY_CARD_LIMIT),
        client_inquiries=await _client_inquiries(session, advisor, settings, _INQUIRIES_CARD_LIMIT),
    )


# ── CSV export ───────────────────────────────────────────────────────────────

_CSV_HEADERS = ("metric", "value")


async def export_dashboard_csv(
    session: AsyncSession,
    advisor: User,
    profile: AdvisorProfile,
    settings: Settings,
    days: int,
) -> str:
    """Flat metric/value CSV of the windowed stats + next appointment summary."""
    dashboard = await get_dashboard(session, advisor, profile, settings, days)
    stats = dashboard.stats
    upcoming = dashboard.next_upcoming

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_HEADERS)
    writer.writerow(("window_days", days))
    writer.writerow(("new_leads_count", stats.new_leads_count))
    writer.writerow(("total_earned_usd", f"{stats.total_earned_usd:.2f}"))
    writer.writerow(("pending_reviews_count", stats.pending_reviews_count))
    writer.writerow(("profile_completion_percent", stats.profile_completion_percent))
    writer.writerow(
        (
            "next_appointment",
            (
                f"{upcoming.appointment_id} with {upcoming.seeker_name or 'client'} "
                f"at {upcoming.scheduled_start.strftime('%Y-%m-%d %H:%M UTC')}"
                if upcoming
                else "none"
            ),
        )
    )
    return buf.getvalue()


async def get_regulatory_update(
    session: AsyncSession, update_id: uuid.UUID
) -> RegulatoryUpdate | None:
    update = await session.get(RegulatoryUpdate, update_id)
    if update is None or update.is_archived:
        return None
    return update
