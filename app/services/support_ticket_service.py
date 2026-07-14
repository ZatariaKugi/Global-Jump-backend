"""Admin support ticket CRUD and lifecycle (PRD §4.6 Support & Moderation)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.support_ticket import (
    SupportTicket,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from app.models.user import User
from app.schemas.support_ticket import TicketCreate, TicketRead, TicketUpdate

_RESOLVED_STATUSES = (TicketStatus.resolved, TicketStatus.closed)


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


async def ticket_read(session: AsyncSession, ticket: SupportTicket) -> TicketRead:
    user = await session.get(User, ticket.user_id)
    return TicketRead(
        id=ticket.id,
        user_id=ticket.user_id,
        user_name=user.full_name if user else None,
        subject=ticket.subject,
        description=ticket.description,
        category=ticket.category,
        priority=ticket.priority,
        status=ticket.status,
        preferred_contact_at=ticket.preferred_contact_at,
        resolved_at=ticket.resolved_at,
        resolved_by=ticket.resolved_by,
        created_at=ticket.created_at,
    )
