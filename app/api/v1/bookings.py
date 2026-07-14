"""Consultation booking endpoints (PRD §3.6)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.exceptions import PermissionDeniedError
from app.core.file_storage import resolve_url
from app.db.session import SessionDep
from app.models.booking import Booking, BookingStatus
from app.models.booking_note import BookingNoteAttachment
from app.models.user import User
from app.schemas.booking import (
    BookingCancel,
    BookingCreate,
    BookingImportantUpdate,
    BookingInterpreterUpdate,
    BookingRead,
    BookingReject,
    BookingReschedule,
)
from app.schemas.booking_document_request import (
    DocumentRequestCreate,
    DocumentRequestFulfill,
    DocumentRequestRead,
)
from app.schemas.booking_note import BookingNoteCreate, BookingNoteRead
from app.schemas.response import Meta, ResponseEnvelope
from app.services import (
    booking_document_service,
    booking_note_service,
    booking_service,
    email_service,
)
from app.services.availability_service import as_utc
from app.services.booking_service import get_notice_hours

router = APIRouter(prefix="/bookings", tags=["bookings"])


async def _party_names(session: SessionDep, booking: Booking) -> tuple[User | None, User | None]:
    seeker = await session.get(User, booking.seeker_id)
    advisor = await session.get(User, booking.advisor_id)
    return seeker, advisor


def _read(booking: Booking, seeker: User | None, advisor: User | None) -> BookingRead:
    return BookingRead(
        id=booking.id,
        seeker_id=booking.seeker_id,
        advisor_id=booking.advisor_id,
        seeker_name=seeker.full_name if seeker else None,
        advisor_name=advisor.full_name if advisor else None,
        service_type=booking.service_type,
        duration_minutes=booking.duration_minutes,
        price_usd=booking.price_usd,
        scheduled_start=as_utc(booking.scheduled_start),
        scheduled_end=as_utc(booking.scheduled_end),
        status=booking.status,
        payment_status=booking.payment_status,
        cancellation_reason=booking.cancellation_reason,
        seeker_note=booking.seeker_note,
        is_important=booking.is_important,
        interpreter_name=booking.interpreter_name,
        interpreter_contact=booking.interpreter_contact,
        interpreter_language=booking.interpreter_language,
        created_at=booking.created_at,
    )


async def _send_confirmations(session: SessionDep, booking: Booking, settings: SettingsDep) -> None:
    seeker, advisor = await _party_names(session, booking)
    notice = await get_notice_hours(session, booking.advisor_id)
    for recipient, other in ((seeker, advisor), (advisor, seeker)):
        if recipient is None:
            continue
        await email_service.send_booking_confirmation_email(
            recipient.email,
            recipient.full_name or recipient.email,
            (other.full_name or other.email) if other else "your counterpart",
            booking_id=str(booking.id),
            service_type=booking.service_type,
            start_utc=as_utc(booking.scheduled_start),
            end_utc=as_utc(booking.scheduled_end),
            duration_minutes=booking.duration_minutes,
            price_usd=booking.price_usd,
            notice_hours=notice,
            settings=settings,
        )


async def _send_new_request_notification(
    session: SessionDep, booking: Booking, settings: SettingsDep
) -> None:
    seeker, advisor = await _party_names(session, booking)
    if advisor is None:
        return
    await email_service.send_new_consultation_request_email(
        advisor.email,
        advisor.full_name or advisor.email,
        (seeker.full_name or seeker.email) if seeker else "a seeker",
        booking_id=str(booking.id),
        service_type=booking.service_type,
        start_utc=as_utc(booking.scheduled_start),
        settings=settings,
    )


async def _send_rejection_notification(
    session: SessionDep, booking: Booking, settings: SettingsDep
) -> None:
    seeker, advisor = await _party_names(session, booking)
    if seeker is None:
        return
    await email_service.send_booking_rejected_email(
        seeker.email,
        seeker.full_name or seeker.email,
        (advisor.full_name or advisor.email) if advisor else "the advisor",
        booking_id=str(booking.id),
        service_type=booking.service_type,
        start_utc=as_utc(booking.scheduled_start),
        reason=booking.cancellation_reason,
        settings=settings,
    )


@router.post("", status_code=201, response_model=ResponseEnvelope[BookingRead])
async def create_booking(
    data: BookingCreate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.create(session, current_user, data)
    await _send_new_request_notification(session, booking, settings)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.get("", response_model=ResponseEnvelope[list[BookingRead]])
async def list_my_bookings(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    status: BookingStatus | None = None,
    seeker_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    service_type: Annotated[list[str] | None, Query()] = None,
) -> ResponseEnvelope[list[BookingRead]]:
    role = current_user.role
    stmt = booking_service.list_for_user_stmt(
        current_user.id, role, status, seeker_id, date_from, date_to, service_type
    )
    bookings, total = await paginate(session, stmt, params)

    user_ids = {b.seeker_id for b in bookings} | {b.advisor_id for b in bookings}
    users: dict[uuid.UUID, User] = {}
    for uid in user_ids:
        user = await session.get(User, uid)
        if user is not None:
            users[uid] = user

    return ResponseEnvelope[list[BookingRead]](
        data=[_read(b, users.get(b.seeker_id), users.get(b.advisor_id)) for b in bookings],
        meta=page_meta(params, total, request_id),
    )


@router.get("/{booking_id}", response_model=ResponseEnvelope[BookingRead])
async def get_booking(
    booking_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.post("/{booking_id}/accept", response_model=ResponseEnvelope[BookingRead])
async def accept_booking(
    booking_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    booking = await booking_service.accept(session, booking, current_user.id)
    await _send_confirmations(session, booking, settings)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.post("/{booking_id}/reject", response_model=ResponseEnvelope[BookingRead])
async def reject_booking(
    booking_id: uuid.UUID,
    data: BookingReject,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    booking = await booking_service.reject(session, booking, current_user.id, data.reason)
    await _send_rejection_notification(session, booking, settings)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.patch("/{booking_id}/important", response_model=ResponseEnvelope[BookingRead])
async def update_booking_important(
    booking_id: uuid.UUID,
    data: BookingImportantUpdate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    booking = await booking_service.set_important(
        session, booking, current_user.id, data.is_important
    )
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.put("/{booking_id}/interpreter", response_model=ResponseEnvelope[BookingRead])
async def update_booking_interpreter(
    booking_id: uuid.UUID,
    data: BookingInterpreterUpdate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    booking = await booking_service.set_interpreter(
        session, booking, current_user.id, data.name, data.contact, data.language
    )
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.post("/{booking_id}/cancel", response_model=ResponseEnvelope[BookingRead])
async def cancel_booking(
    booking_id: uuid.UUID,
    data: BookingCancel,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    booking = await booking_service.cancel(session, booking, current_user.id, data.reason)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.post("/{booking_id}/reschedule", response_model=ResponseEnvelope[BookingRead])
async def reschedule_booking(
    booking_id: uuid.UUID,
    data: BookingReschedule,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    booking = await booking_service.reschedule(
        session, booking, current_user.id, data.scheduled_start
    )
    await _send_confirmations(session, booking, settings)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.post("/{booking_id}/complete", response_model=ResponseEnvelope[BookingRead])
async def complete_booking(
    booking_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    booking = await booking_service.complete(session, booking, current_user.id)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.post("/{booking_id}/no-show", response_model=ResponseEnvelope[BookingRead])
async def mark_booking_no_show(
    booking_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    booking = await booking_service.mark_no_show(session, booking, current_user.id)
    seeker, advisor = await _party_names(session, booking)
    return ResponseEnvelope[BookingRead](
        data=_read(booking, seeker, advisor),
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/{booking_id}/notes", status_code=201, response_model=ResponseEnvelope[BookingNoteRead]
)
async def create_booking_note(
    booking_id: uuid.UUID,
    data: BookingNoteCreate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookingNoteRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)

    attachments: list[BookingNoteAttachment] = []
    expected_prefix = f"booking_note/{current_user.id}/"
    for ref in data.attachments:
        if not ref.file_key.startswith(expected_prefix):
            raise PermissionDeniedError("Invalid attachment key")
        file_url = resolve_url(f"/uploads/{ref.file_key}", settings)
        attachments.append(
            BookingNoteAttachment(
                file_url=file_url,
                file_name=ref.file_name,
                file_size=ref.file_size_bytes,
                content_type=ref.content_type,
            )
        )

    note = await booking_note_service.create_note(
        session, booking, current_user, data.body, attachments
    )
    return ResponseEnvelope[BookingNoteRead](
        data=booking_note_service.build_read(note, current_user, settings),
        meta=Meta(request_id=request_id),
    )


@router.get("/{booking_id}/notes", response_model=ResponseEnvelope[list[BookingNoteRead]])
async def list_booking_notes(
    booking_id: uuid.UUID,
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[BookingNoteRead]]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    stmt = booking_note_service.list_for_booking_stmt(booking.id)
    notes, total = await paginate(session, stmt, params)

    authors: dict[uuid.UUID, User] = {}
    for note in notes:
        if note.author_id not in authors:
            author = await session.get(User, note.author_id)
            if author is not None:
                authors[note.author_id] = author

    return ResponseEnvelope[list[BookingNoteRead]](
        data=[
            booking_note_service.build_read(n, authors.get(n.author_id), settings) for n in notes
        ],
        meta=page_meta(params, total, request_id),
    )


@router.post(
    "/{booking_id}/document-requests",
    status_code=201,
    response_model=ResponseEnvelope[DocumentRequestRead],
)
async def create_document_request(
    booking_id: uuid.UUID,
    data: DocumentRequestCreate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[DocumentRequestRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    request = await booking_document_service.create_request(
        session, booking, current_user.id, data.description
    )
    return ResponseEnvelope[DocumentRequestRead](
        data=booking_document_service.build_read(request, settings),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/{booking_id}/document-requests",
    response_model=ResponseEnvelope[list[DocumentRequestRead]],
)
async def list_document_requests(
    booking_id: uuid.UUID,
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[DocumentRequestRead]]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    stmt = booking_document_service.list_for_booking_stmt(booking.id)
    requests, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[DocumentRequestRead]](
        data=[booking_document_service.build_read(r, settings) for r in requests],
        meta=page_meta(params, total, request_id),
    )


@router.post(
    "/{booking_id}/document-requests/{request_id_}/fulfill",
    response_model=ResponseEnvelope[DocumentRequestRead],
)
async def fulfill_document_request(
    booking_id: uuid.UUID,
    request_id_: uuid.UUID,
    data: DocumentRequestFulfill,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[DocumentRequestRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    doc_request = await booking_document_service.get_for_booking(session, booking.id, request_id_)

    expected_prefix = f"booking_document/{current_user.id}/"
    if not data.file_key.startswith(expected_prefix):
        raise PermissionDeniedError("Invalid attachment key")
    file_url = resolve_url(f"/uploads/{data.file_key}", settings)

    doc_request = await booking_document_service.fulfill(
        session,
        doc_request,
        booking,
        current_user.id,
        file_url=file_url,
        file_name=data.file_name,
        file_size=data.file_size_bytes,
        content_type=data.content_type,
    )
    return ResponseEnvelope[DocumentRequestRead](
        data=booking_document_service.build_read(doc_request, settings),
        meta=Meta(request_id=request_id),
    )
