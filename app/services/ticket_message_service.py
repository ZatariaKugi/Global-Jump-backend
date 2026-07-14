"""A support ticket's conversation thread (PRD §4.6 Support & Moderation)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.file_storage import resolve_url
from app.models.support_ticket import SupportTicket
from app.models.ticket_message import TicketMessage, TicketMessageAttachment
from app.models.user import User
from app.schemas.ticket_message import TicketAttachmentRead, TicketMessageRead


def list_messages_stmt(ticket_id: uuid.UUID) -> Select[tuple[TicketMessage]]:
    return (
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.created_at.asc())
    )


async def send_message(
    session: AsyncSession,
    ticket: SupportTicket,
    sender: User,
    body: str | None,
    attachments: list[TicketMessageAttachment] | None = None,
) -> TicketMessage:
    body = body.strip() if body else None
    attachments = attachments or []
    if not body and not attachments:
        raise AppError("Message must contain text or an attachment", code="empty_message")

    message = TicketMessage(
        ticket_id=ticket.id,
        sender_id=sender.id,
        body=body,
        created_by=sender.id,
        created_at=datetime.now(UTC),  # explicit so message order is unambiguous
    )
    message.attachments = attachments
    session.add(message)
    await session.flush()
    await session.refresh(message)
    return message


def build_message_read(
    message: TicketMessage, sender: User | None, settings: Settings
) -> TicketMessageRead:
    return TicketMessageRead(
        id=message.id,
        ticket_id=message.ticket_id,
        sender_id=message.sender_id,
        sender_name=sender.full_name if sender else None,
        body=message.body,
        attachments=[
            TicketAttachmentRead(
                id=a.id,
                file_url=resolve_url(a.file_url, settings),
                file_name=a.file_name,
                file_size=a.file_size,
                content_type=a.content_type,
            )
            for a in message.attachments
        ],
        created_at=message.created_at,
    )
