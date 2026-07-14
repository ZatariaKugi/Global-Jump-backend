"""Advisor notes attached to a booking — creation, listing, and read-model building."""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, PermissionDeniedError
from app.core.file_storage import resolve_url
from app.models.booking import Booking
from app.models.booking_note import BookingNote, BookingNoteAttachment
from app.models.user import User
from app.schemas.booking_note import BookingNoteAttachmentRead, BookingNoteRead


async def create_note(
    session: AsyncSession,
    booking: Booking,
    author: User,
    body: str | None,
    attachments: list[BookingNoteAttachment] | None = None,
) -> BookingNote:
    if author.id != booking.advisor_id:
        raise PermissionDeniedError("Only the advisor can add a note")

    body = body.strip() if body else None
    attachments = attachments or []
    if not body and not attachments:
        raise AppError("Note must contain text or an attachment", code="empty_note")

    note = BookingNote(
        booking_id=booking.id,
        author_id=author.id,
        body=body,
        created_by=author.id,
    )
    note.attachments = attachments
    session.add(note)
    await session.flush()
    await session.refresh(note)
    return note


def list_for_booking_stmt(booking_id: uuid.UUID) -> Select[tuple[BookingNote]]:
    return (
        select(BookingNote)
        .where(BookingNote.booking_id == booking_id)
        .order_by(BookingNote.created_at.desc())
    )


def build_read(note: BookingNote, author: User | None, settings: Settings) -> BookingNoteRead:
    return BookingNoteRead(
        id=note.id,
        booking_id=note.booking_id,
        author_id=note.author_id,
        author_name=author.full_name if author else None,
        body=note.body,
        attachments=[
            BookingNoteAttachmentRead(
                id=a.id,
                file_url=resolve_url(a.file_url, settings),
                file_name=a.file_name,
                file_size=a.file_size,
                content_type=a.content_type,
            )
            for a in note.attachments
        ],
        created_at=note.created_at,
    )
