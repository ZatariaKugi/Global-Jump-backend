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
from app.models.ticket_message import TicketMessage, TicketMessageAttachment
from app.models.user import User, UserRole
from app.schemas.support_ticket import TicketCreate, TicketRead, TicketUpdate
from app.schemas.ticket_message import TicketAttachmentRead

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

_CATEGORY_ALIASES: dict[str, TicketCategory] = {
    "payment": TicketCategory.billing,
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


def coerce_category_filter(raw: str | TicketCategory | None) -> TicketCategory | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, TicketCategory):
        return raw
    key = raw.strip().lower()
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    return TicketCategory(key)


async def _resolve_ticket_user(session: AsyncSession, data: TicketCreate) -> User:
    if data.user_id is not None:
        user = await session.get(User, data.user_id)
        if user is None:
            raise NotFoundError("User not found")
        return user
    assert data.user_email is not None
    user = (
        await session.execute(select(User).where(User.email == data.user_email))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")
    return user


async def create(
    session: AsyncSession,
    data: TicketCreate,
    admin_id: uuid.UUID,
    *,
    opening_attachments: list[TicketMessageAttachment] | None = None,
) -> SupportTicket:
    user = await _resolve_ticket_user(session, data)

    if data.assigned_to is not None:
        assignee = await session.get(User, data.assigned_to)
        if assignee is None:
            raise NotFoundError("Assignee not found")

    ticket = SupportTicket(
        user_id=user.id,
        subject=data.subject,
        description=data.description,
        category=data.category,
        priority=data.priority,
        preferred_contact_at=data.preferred_contact_at,
        assigned_to=data.assigned_to,
        internal_notes=data.internal_notes,
        created_by=admin_id,
    )
    session.add(ticket)
    await session.flush()

    attachments = opening_attachments or []
    if attachments or data.description:
        # Opening thread message so create-time files appear in the conversation.
        message = TicketMessage(
            ticket_id=ticket.id,
            sender_id=admin_id,
            body=data.description,
            created_by=admin_id,
            created_at=datetime.now(UTC),
        )
        message.attachments = attachments
        session.add(message)
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
    fields = data.model_dump(exclude_unset=True)
    if "assigned_to" in fields:
        assignee_id = fields["assigned_to"]
        if assignee_id is not None:
            assignee = await session.get(User, assignee_id)
            if assignee is None:
                raise NotFoundError("Assignee not found")
        ticket.assigned_to = assignee_id
        fields.pop("assigned_to")
    if "status" in fields:
        status = fields.pop("status")
        ticket.status = status
        if status in _RESOLVED_STATUSES:
            ticket.resolved_at = datetime.now(UTC)
            ticket.resolved_by = admin_id
        else:
            ticket.resolved_at = None
            ticket.resolved_by = None
    for field, value in fields.items():
        setattr(ticket, field, value)
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
    assignee_ids = {t.assigned_to for t in tickets if t.assigned_to is not None}
    resolver_ids = {t.resolved_by for t in tickets if t.resolved_by is not None}
    all_user_ids = user_ids | assignee_ids | resolver_ids

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
    attachments_by_ticket: dict[uuid.UUID, list[TicketAttachmentRead]] = {
        tid: [] for tid in ticket_ids
    }
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

        messages = (
            await session.execute(
                select(TicketMessage).where(TicketMessage.ticket_id.in_(ticket_ids))
            )
        ).scalars().all()
        for message in messages:
            for att in message.attachments or []:
                attachments_by_ticket[message.ticket_id].append(
                    TicketAttachmentRead(
                        id=att.id,
                        file_url=resolve_url(att.file_url, settings),
                        file_name=att.file_name,
                        file_size=att.file_size,
                        content_type=att.content_type,
                    )
                )

    out: list[TicketRead] = []
    for ticket in tickets:
        user = users.get(ticket.user_id)
        assignee = users.get(ticket.assigned_to) if ticket.assigned_to else None
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
                internal_notes=ticket.internal_notes,
                resolved_at=ticket.resolved_at,
                resolved_by=ticket.resolved_by,
                assigned_to=ticket.assigned_to,
                assigned_to_name=assignee.full_name if assignee else None,
                total_responses=total,
                last_response_at=last_at,
                attachments=attachments_by_ticket.get(ticket.id, []),
                created_at=ticket.created_at,
                updated_at=ticket.updated_at,
            )
        )
    return out
