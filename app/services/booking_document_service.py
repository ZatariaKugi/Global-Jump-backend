"""Advisor document requests on a booking — creation, listing, fulfillment."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from app.core.file_storage import resolve_url
from app.models.booking import Booking
from app.models.booking_document_request import BookingDocumentRequest, DocumentRequestStatus
from app.schemas.booking_document_request import DocumentRequestRead


async def create_request(
    session: AsyncSession, booking: Booking, actor_id: uuid.UUID, description: str
) -> BookingDocumentRequest:
    if actor_id != booking.advisor_id:
        raise PermissionDeniedError("Only the advisor can request documents")
    request = BookingDocumentRequest(
        booking_id=booking.id,
        requested_by=actor_id,
        description=description,
        created_by=actor_id,
    )
    session.add(request)
    await session.flush()
    await session.refresh(request)
    return request


def list_for_booking_stmt(booking_id: uuid.UUID) -> Select[tuple[BookingDocumentRequest]]:
    return (
        select(BookingDocumentRequest)
        .where(BookingDocumentRequest.booking_id == booking_id)
        .order_by(BookingDocumentRequest.created_at.desc())
    )


async def get_for_booking(
    session: AsyncSession, booking_id: uuid.UUID, request_id: uuid.UUID
) -> BookingDocumentRequest:
    request = await session.get(BookingDocumentRequest, request_id)
    if request is None or request.booking_id != booking_id:
        raise NotFoundError("Document request not found")
    return request


async def fulfill(
    session: AsyncSession,
    request: BookingDocumentRequest,
    booking: Booking,
    actor_id: uuid.UUID,
    *,
    file_url: str,
    file_name: str,
    file_size: int,
    content_type: str,
) -> BookingDocumentRequest:
    if actor_id != booking.seeker_id:
        raise PermissionDeniedError("Only the seeker can fulfill this request")
    if request.status != DocumentRequestStatus.requested:
        raise AppError("Document request is already fulfilled", code="invalid_state")

    request.file_url = file_url
    request.file_name = file_name
    request.file_size = file_size
    request.content_type = content_type
    request.status = DocumentRequestStatus.fulfilled
    request.fulfilled_at = datetime.now(UTC)
    request.updated_by = actor_id
    session.add(request)
    await session.flush()
    await session.refresh(request)
    return request


def build_read(request: BookingDocumentRequest, settings: Settings) -> DocumentRequestRead:
    return DocumentRequestRead(
        id=request.id,
        booking_id=request.booking_id,
        requested_by=request.requested_by,
        description=request.description,
        status=request.status,
        file_url=resolve_url(request.file_url, settings) if request.file_url else None,
        file_name=request.file_name,
        file_size=request.file_size,
        content_type=request.content_type,
        fulfilled_at=request.fulfilled_at,
        created_at=request.created_at,
    )
