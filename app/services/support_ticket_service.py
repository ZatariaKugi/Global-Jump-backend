"""Admin support ticket CRUD and lifecycle (PRD §4.6 Support & Moderation)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.file_storage import resolve_url
from app.models.advisor_profile import AdvisorProfile
from app.models.seeker_profile import SeekerProfile
from app.models.support_ticket import (
    SupportTicket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from app.models.ticket_message import TicketMessage
from app.models.user import User, UserRole
from app.schemas.support_ticket import TicketCreate, TicketRead, TicketUpdate

_RESOLVED_STATUSES = (TicketStatus.resolved, TicketStatus.closed)

# Frontend filter labels → canonical TicketStatus (list query only).
_STATUS_ALIASES: dict[str, TicketStatus] = {
    "pending": TicketStatus.open,
    "inprogress": TicketStatus.in_progress,
    "in_progress": TicketStatus.in_progress,
    "escalated": TicketStatus.in_progress,
    "open": TicketStatus.open,
    "resolved": TicketStatus.resolved,
    "closed": TicketStatus.closed,
}


def ticket_number_for(ticket_id: uuid.UUID) -> str:
    """Human-readable id derived from the UUID (no extra DB column)."""
    return f"TKT-{ticket_id.hex[:8].upper()}"


def coerce_status_filter(raw: str | TicketStatus | None) -> TicketStatus | None:
    """Map FE aliases (pending / inprogress / escalated) to stored enum values."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, TicketStatus):
        return raw
    key = raw.strip().lower().replace("-", "_")
    if key in _STATUS_ALIASES:
        return _STATUS_ALIASES[key]
    raise ValueError(f"Invalid ticket status filter: {raw}")


async def create(
    session: AsyncSession, data: TicketCreate, admin_id: uuid.UUID
) -> SupportTicket:
    user = await session.get(User, data.user_id)
    if user is None:
        raise NotFoundError("User not found")

    ticket = SupportTicket(
        user_id=data.user_id,
        subject=data.subject,
        description=data.description,
        category=data.category,
        priority=data.priority,
        preferred_contact_at=data.preferred_contact_at,
        created_by=admin_id,
    )
    session.add(ticket)
    await session.flush()
    await session.refresh(ticket)
    return ticket


def list_stmt(
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    category: TicketCategory | None = None,
    search: str | None = None,
) -> Select[tuple[SupportTicket]]:
    stmt = select(SupportTicket)
    if status is not None:
        stmt = stmt.where(SupportTicket.status == status)
    if priority is not None:
        stmt = stmt.where(SupportTicket.priority == priority)
    if category is not None:
        stmt = stmt.where(SupportTicket.category == category)
    if search:
        stmt = stmt.join(User, User.id == SupportTicket.user_id).where(
            or_(
                SupportTicket.subject.ilike(f"%{search}%"),
                User.full_name.ilike(f"%{search}%"),
                User.email.ilike(f"%{search}%"),
            )
        )
    return stmt.order_by(SupportTicket.created_at.desc())


def list_for_user_stmt(user_id: uuid.UUID) -> Select[tuple[SupportTicket]]:
    return (
        select(SupportTicket)
        .where(SupportTicket.user_id == user_id)
        .order_by(SupportTicket.created_at.desc())
    )


async def get_by_id(session: AsyncSession, ticket_id: uuid.UUID) -> SupportTicket:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise NotFoundError("Ticket not found")
    return ticket


async def get_for_user(
    session: AsyncSession, ticket_id: uuid.UUID, user_id: uuid.UUID
) -> SupportTicket:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None or ticket.user_id != user_id:
        raise NotFoundError("Ticket not found")
    return ticket


async def update(
    session: AsyncSession, ticket: SupportTicket, data: TicketUpdate, admin_id: uuid.UUID
) -> SupportTicket:
    if data.category is not None:
        ticket.category = data.category
    if data.priority is not None:
        ticket.priority = data.priority
    if data.status is not None:
        ticket.status = data.status
        if data.status in _RESOLVED_STATUSES:
            ticket.resolved_at = datetime.now(UTC)
            ticket.resolved_by = admin_id
        else:
            ticket.resolved_at = None
            ticket.resolved_by = None
    ticket.updated_by = admin_id
    session.add(ticket)
    await session.flush()
    await session.refresh(ticket)
    return ticket


async def ticket_read(
    session: AsyncSession, ticket: SupportTicket, settings: Settings
) -> TicketRead:
    rows = await build_list_reads(session, [ticket], settings)
    return rows[0]


async def build_list_reads(
    session: AsyncSession, tickets: list[SupportTicket], settings: Settings
) -> list[TicketRead]:
    """Bulk-enrich tickets for list/detail without per-row queries."""
    if not tickets:
        return []

    user_ids = {t.user_id for t in tickets}
    resolver_ids = {t.resolved_by for t in tickets if t.resolved_by is not None}
    all_user_ids = user_ids | resolver_ids

    users: dict[uuid.UUID, User] = {}
    if all_user_ids:
        rows = (
            await session.execute(select(User).where(User.id.in_(all_user_ids)))
        ).scalars().all()
        users = {u.id: u for u in rows}

    advisor_photos: dict[uuid.UUID, str | None] = {}
    seeker_photos: dict[uuid.UUID, str | None] = {}
    if user_ids:
        for adv in (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id.in_(user_ids))
            )
        ).scalars().all():
            advisor_photos[adv.user_id] = adv.profile_photo_url
        for seeker in (
            await session.execute(
                select(SeekerProfile).where(SeekerProfile.user_id.in_(user_ids))
            )
        ).scalars().all():
            seeker_photos[seeker.user_id] = seeker.profile_photo_url

    ticket_ids = [t.id for t in tickets]
    msg_stats: dict[uuid.UUID, tuple[int, datetime | None]] = dict.fromkeys(
        ticket_ids, (0, None)
    )
    if ticket_ids:
        agg = (
            await session.execute(
                select(
                    TicketMessage.ticket_id,
                    func.count(TicketMessage.id),
                    func.max(TicketMessage.created_at),
                )
                .where(TicketMessage.ticket_id.in_(ticket_ids))
                .group_by(TicketMessage.ticket_id)
            )
        ).all()
        for ticket_id, count, last_at in agg:
            msg_stats[ticket_id] = (int(count), last_at)

    out: list[TicketRead] = []
    for ticket in tickets:
        user = users.get(ticket.user_id)
        assignee = users.get(ticket.resolved_by) if ticket.resolved_by else None
        total, last_at = msg_stats[ticket.id]

        avatar_raw: str | None = None
        user_type: UserRole | None = user.role if user else None
        if user is not None:
            if user.role == UserRole.advisor:
                avatar_raw = advisor_photos.get(user.id)
            elif user.role == UserRole.seeker:
                avatar_raw = seeker_photos.get(user.id)

        out.append(
            TicketRead(
                id=ticket.id,
                ticket_number=ticket_number_for(ticket.id),
                user_id=ticket.user_id,
                user_name=user.full_name if user else None,
                user_email=user.email if user else None,
                user_type=user_type,
                avatar_url=resolve_url(avatar_raw, settings) if avatar_raw else None,
                phone=None,
                subject=ticket.subject,
                description=ticket.description,
                category=ticket.category,
                priority=ticket.priority,
                status=ticket.status,
                preferred_contact_at=ticket.preferred_contact_at,
                resolved_at=ticket.resolved_at,
                resolved_by=ticket.resolved_by,
                assigned_to=ticket.resolved_by,
                assigned_to_name=assignee.full_name if assignee else None,
                total_responses=total,
                last_response_at=last_at,
                created_at=ticket.created_at,
                updated_at=ticket.updated_at,
            )
        )
    return out
