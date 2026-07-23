"""Booking lifecycle — instant booking, cancellation policy, state transitions."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import String

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from app.core.file_storage import resolve_media_url
from app.core.visa_types import humanize_slug
from app.models.advisor_lead import AdvisorLead, AdvisorLeadStatus
from app.models.advisor_profile import AdvisorProfile, AdvisorService
from app.models.booking import APPOINTMENT_NUMBER_START, Booking, BookingStatus, PaymentStatus
from app.models.booking_document_request import DocumentRequestStatus
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.booking import (
    AdvisorBookingCreate,
    BookingAiSuggestionRead,
    BookingAttachmentRead,
    BookingCreate,
    BookingDetailsRead,
    BookingHistoryRead,
    BookingMeetingRead,
    BookingRead,
    BookingSort,
    ClientRead,
)
from app.services import availability_service, booking_document_service, booking_note_service
from app.services.availability_service import as_utc

DEFAULT_NOTICE_HOURS = 24
_ACTIVE_UPCOMING = (BookingStatus.pending, BookingStatus.confirmed)


async def _next_appointment_number(session: AsyncSession) -> int:
    """Allocate the next human-readable Appointment ID (SQLite- and Postgres-safe)."""
    result = await session.execute(select(func.max(Booking.appointment_number)))
    current = result.scalar_one_or_none()
    if current is None:
        return APPOINTMENT_NUMBER_START
    return int(current) + 1


def appointment_id_str(booking: Booking) -> str:
    return str(booking.appointment_number)


def build_read(
    booking: Booking,
    seeker: User | None,
    advisor: User | None,
    *,
    settings: Settings,
    advisor_profile_photo_key: str | None = None,
    seeker_profile_photo_key: str | None = None,
) -> BookingRead:
    platform_fee = round(booking.price_usd * settings.PLATFORM_COMMISSION_RATE, 2)
    advisor_fee = round(booking.price_usd - platform_fee, 2)
    return BookingRead(
        id=booking.id,
        appointment_id=appointment_id_str(booking),
        seeker_id=booking.seeker_id,
        advisor_id=booking.advisor_id,
        seeker_name=seeker.full_name if seeker else None,
        seeker_email=seeker.email if seeker else None,
        seeker_profile_photo_url=resolve_media_url(seeker_profile_photo_key, settings),
        advisor_name=advisor.full_name if advisor else None,
        advisor_email=advisor.email if advisor else None,
        advisor_profile_photo_url=resolve_media_url(advisor_profile_photo_key, settings),
        service_type=booking.service_type,
        duration_minutes=booking.duration_minutes,
        advisor_fee_usd=advisor_fee,
        platform_fee_usd=platform_fee,
        price_usd=booking.price_usd,
        scheduled_start=as_utc(booking.scheduled_start),
        scheduled_end=as_utc(booking.scheduled_end),
        status=booking.status,
        payment_status=booking.payment_status,
        cancellation_reason=booking.cancellation_reason,
        seeker_note=booking.seeker_note,
        deal_later_at=as_utc(booking.deal_later_at) if booking.deal_later_at else None,
        is_important=booking.is_important,
        interpreter_name=booking.interpreter_name,
        interpreter_contact=booking.interpreter_contact,
        interpreter_language=booking.interpreter_language,
        created_at=booking.created_at,
        updated_at=booking.updated_at,
    )


async def advisor_photo_keys(
    session: AsyncSession, advisor_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    if not advisor_ids:
        return {}
    rows = (
        await session.execute(
            select(AdvisorProfile.user_id, AdvisorProfile.profile_photo_url).where(
                AdvisorProfile.user_id.in_(advisor_ids)
            )
        )
    ).all()
    out: dict[uuid.UUID, str | None] = {}
    for user_id, photo in rows:
        out[user_id] = photo
    return out


async def seeker_photo_keys(
    session: AsyncSession, seeker_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    if not seeker_ids:
        return {}
    rows = (
        await session.execute(
            select(SeekerProfile.user_id, SeekerProfile.profile_photo_url).where(
                SeekerProfile.user_id.in_(seeker_ids)
            )
        )
    ).all()
    out: dict[uuid.UUID, str | None] = {}
    for user_id, photo in rows:
        out[user_id] = photo
    return out


async def _resolve_advisor(session: AsyncSession, advisor_id: uuid.UUID) -> User:
    advisor = await session.get(User, advisor_id)
    if (
        advisor is None
        or advisor.role != UserRole.advisor
        or not advisor.is_active
        or advisor.verification_status != VerificationStatus.approved
    ):
        raise NotFoundError("Advisor not found")
    return advisor


async def _resolve_service(
    session: AsyncSession, advisor_id: uuid.UUID, service_type: str
) -> AdvisorService:
    result = await session.execute(
        select(AdvisorService)
        .join(AdvisorProfile, AdvisorProfile.id == AdvisorService.profile_id)
        .where(AdvisorProfile.user_id == advisor_id)
        .where(AdvisorService.service_type == service_type)
    )
    service = result.scalars().first()
    if service is None:
        raise AppError("Advisor does not offer this service", code="unknown_service")
    return service


async def get_notice_hours(session: AsyncSession, advisor_id: uuid.UUID) -> int:
    result = await session.execute(
        select(AdvisorProfile.cancellation_notice_hours).where(AdvisorProfile.user_id == advisor_id)
    )
    hours = result.scalar_one_or_none()
    return hours if hours is not None else DEFAULT_NOTICE_HOURS


async def _assert_slot_free(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    start_utc: datetime,
    duration_minutes: int,
) -> datetime:
    """Validate the requested start against the advisor's free slots; return end."""
    free = await availability_service.free_slots(
        session, advisor_id, start_utc.date(), start_utc.date(), duration_minutes
    )
    for slot_start, slot_end in free:
        if slot_start == start_utc:
            return slot_end
    raise AppError("Requested time is not available", code="slot_unavailable")


async def create(session: AsyncSession, seeker: User, data: BookingCreate) -> Booking:
    if seeker.role != UserRole.seeker:
        raise PermissionDeniedError("Seeker account required")
    if seeker.id == data.advisor_id:
        raise AppError("Cannot book yourself", code="invalid_booking")

    await _resolve_advisor(session, data.advisor_id)
    service = await _resolve_service(session, data.advisor_id, str(data.service_type))

    start_utc = as_utc(data.scheduled_start)
    if start_utc <= datetime.now(UTC):
        raise AppError("Booking must be in the future", code="invalid_booking")

    end_utc = await _assert_slot_free(session, data.advisor_id, start_utc, service.duration_minutes)

    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=data.advisor_id,
        appointment_number=await _next_appointment_number(session),
        service_type=service.service_type,
        duration_minutes=service.duration_minutes,
        price_usd=service.price_usd,
        scheduled_start=start_utc,
        scheduled_end=end_utc,
        status=BookingStatus.pending,  # advisor must accept/reject before it's confirmed
        # Surface new requests on the Clients table (bookmark / red-dot).
        is_important=True,
        seeker_note=data.seeker_note,
        created_by=seeker.id,
    )
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def create_by_advisor(
    session: AsyncSession, advisor: User, data: AdvisorBookingCreate
) -> Booking:
    """Advisor books a consultation directly for one of their clients.

    Confirmed immediately — the advisor is creating and implicitly confirming
    it themselves, so there's no separate accept step (unlike the seeker-initiated
    ``create()`` request-then-approve flow).
    """
    if advisor.id == data.seeker_id:
        raise AppError("Cannot book yourself", code="invalid_booking")

    seeker = await session.get(User, data.seeker_id)
    if seeker is None or seeker.role != UserRole.seeker or not seeker.is_active:
        raise NotFoundError("Client not found")

    service = await _resolve_service(session, advisor.id, str(data.service_type))

    start_utc = as_utc(data.scheduled_start)
    if start_utc <= datetime.now(UTC):
        raise AppError("Booking must be in the future", code="invalid_booking")

    end_utc = await _assert_slot_free(session, advisor.id, start_utc, service.duration_minutes)

    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
        appointment_number=await _next_appointment_number(session),
        service_type=service.service_type,
        duration_minutes=service.duration_minutes,
        price_usd=service.price_usd,
        scheduled_start=start_utc,
        scheduled_end=end_utc,
        status=BookingStatus.confirmed,
        seeker_note=data.seeker_note,
        created_by=advisor.id,
    )
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


def list_for_user_stmt(
    user_id: uuid.UUID,
    role: UserRole,
    status: BookingStatus | None = None,
    seeker_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    service_types: list[str] | None = None,
    q: str | None = None,
    sort: BookingSort = "-updated_at",
) -> Select[tuple[Booking]]:
    column = Booking.advisor_id if role == UserRole.advisor else Booking.seeker_id
    order_map = {
        "scheduled_start": Booking.scheduled_start.asc(),
        "-scheduled_start": Booking.scheduled_start.desc(),
        "updated_at": Booking.updated_at.asc(),
        "-updated_at": Booking.updated_at.desc(),
    }
    stmt = select(Booking).where(column == user_id).order_by(order_map[sort])
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    if seeker_id is not None and role == UserRole.advisor:
        # Advisor-scoped drill-down into one client's history — the outer
        # advisor_id == user_id filter above already prevents seeing anyone
        # else's bookings, this just narrows further to one seeker.
        stmt = stmt.where(Booking.seeker_id == seeker_id)
    if date_from is not None:
        stmt = stmt.where(
            Booking.scheduled_start >= datetime.combine(date_from, datetime.min.time(), UTC)
        )
    if date_to is not None:
        stmt = stmt.where(
            Booking.scheduled_start
            < datetime.combine(date_to + timedelta(days=1), datetime.min.time(), UTC)
        )
    if service_types:
        stmt = stmt.where(Booking.service_type.in_(service_types))
    if q:
        pattern = f"%{q.strip()}%"
        # Advisor searches their clients; seeker searches the advisor side
        # (name/email) — never the seeker's own name when role is seeker.
        counterpart = Booking.seeker_id if role == UserRole.advisor else Booking.advisor_id
        stmt = stmt.join(User, User.id == counterpart).where(
            or_(
                cast(Booking.appointment_number, String).ilike(pattern),
                Booking.service_type.ilike(pattern),
                User.full_name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    return stmt


async def get_next_upcoming(
    session: AsyncSession, user_id: uuid.UUID, role: UserRole
) -> Booking | None:
    """Soonest pending/confirmed booking with ``scheduled_start`` >= now."""
    column = Booking.advisor_id if role == UserRole.advisor else Booking.seeker_id
    now = datetime.now(UTC)
    result = await session.execute(
        select(Booking)
        .where(column == user_id)
        .where(Booking.scheduled_start >= now)
        .where(Booking.status.in_(_ACTIVE_UPCOMING))
        .order_by(Booking.scheduled_start.asc())
        .limit(1)
    )
    return result.scalars().first()


async def get_for_party(
    session: AsyncSession, booking_id: uuid.UUID, user_id: uuid.UUID
) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None or user_id not in (booking.seeker_id, booking.advisor_id):
        raise NotFoundError("Booking not found")
    return booking


def _assert_active(booking: Booking) -> None:
    if booking.status not in (BookingStatus.pending, BookingStatus.confirmed):
        raise AppError("Booking is no longer active", code="invalid_state")


async def _enforce_seeker_notice(
    session: AsyncSession, booking: Booking, actor_id: uuid.UUID
) -> None:
    """Seekers must act outside the advisor's cancellation notice window."""
    if actor_id != booking.seeker_id:
        return  # advisors may act any time
    hours = await get_notice_hours(session, booking.advisor_id)
    deadline = as_utc(booking.scheduled_start) - timedelta(hours=hours)
    if datetime.now(UTC) > deadline:
        raise AppError(
            f"Changes require at least {hours} hours notice",
            code="late_cancellation",
        )


async def cancel(
    session: AsyncSession, booking: Booking, actor_id: uuid.UUID, reason: str | None
) -> Booking:
    _assert_active(booking)
    await _enforce_seeker_notice(session, booking, actor_id)
    booking.status = BookingStatus.cancelled
    booking.cancellation_reason = reason
    booking.cancelled_by = actor_id
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def reschedule(
    session: AsyncSession, booking: Booking, actor_id: uuid.UUID, new_start: datetime
) -> Booking:
    _assert_active(booking)
    await _enforce_seeker_notice(session, booking, actor_id)

    start_utc = as_utc(new_start)
    if start_utc <= datetime.now(UTC):
        raise AppError("Booking must be in the future", code="invalid_booking")
    end_utc = await _assert_slot_free(
        session, booking.advisor_id, start_utc, booking.duration_minutes
    )

    booking.scheduled_start = start_utc
    booking.scheduled_end = end_utc
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def accept(session: AsyncSession, booking: Booking, actor_id: uuid.UUID) -> Booking:
    """Advisor accepts a pending request, confirming the appointment."""
    if actor_id != booking.advisor_id:
        raise PermissionDeniedError("Only the advisor can do this")
    if booking.status != BookingStatus.pending:
        raise AppError("Booking is not pending", code="invalid_state")
    booking.status = BookingStatus.confirmed
    booking.deal_later_at = None
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def reject(
    session: AsyncSession, booking: Booking, actor_id: uuid.UUID, reason: str | None
) -> Booking:
    """Advisor declines a pending request."""
    if actor_id != booking.advisor_id:
        raise PermissionDeniedError("Only the advisor can do this")
    if booking.status != BookingStatus.pending:
        raise AppError("Booking is not pending", code="invalid_state")
    booking.status = BookingStatus.rejected
    booking.cancellation_reason = reason
    booking.cancelled_by = actor_id
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def deal_later(session: AsyncSession, booking: Booking, actor_id: uuid.UUID) -> Booking:
    """Advisor defers Accept/Reject on a pending consultation request (stays pending)."""
    if actor_id != booking.advisor_id:
        raise PermissionDeniedError("Only the advisor can do this")
    if booking.status != BookingStatus.pending:
        raise AppError("Booking is not pending", code="invalid_state")
    booking.deal_later_at = datetime.now(UTC)
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def set_important(
    session: AsyncSession, booking: Booking, actor_id: uuid.UUID, is_important: bool
) -> Booking:
    if actor_id != booking.advisor_id:
        raise PermissionDeniedError("Only the advisor can do this")
    booking.is_important = is_important
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def set_interpreter(
    session: AsyncSession,
    booking: Booking,
    actor_id: uuid.UUID,
    name: str | None,
    contact: str | None,
    language: str | None,
) -> Booking:
    if actor_id != booking.advisor_id:
        raise PermissionDeniedError("Only the advisor can do this")
    booking.interpreter_name = name
    booking.interpreter_contact = contact
    booking.interpreter_language = language
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


def _assert_advisor_post_start(booking: Booking, actor_id: uuid.UUID) -> None:
    if actor_id != booking.advisor_id:
        raise PermissionDeniedError("Only the advisor can do this")
    if booking.status != BookingStatus.confirmed:
        raise AppError("Booking is not confirmed", code="invalid_state")
    if datetime.now(UTC) < as_utc(booking.scheduled_start):
        raise AppError("Session has not started yet", code="invalid_state")


async def complete(session: AsyncSession, booking: Booking, actor_id: uuid.UUID) -> Booking:
    _assert_advisor_post_start(booking, actor_id)
    booking.status = BookingStatus.completed
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def mark_no_show(session: AsyncSession, booking: Booking, actor_id: uuid.UUID) -> Booking:
    _assert_advisor_post_start(booking, actor_id)
    booking.status = BookingStatus.no_show
    booking.updated_by = actor_id
    session.add(booking)
    await session.flush()
    await session.refresh(booking)
    return booking


async def build_history(
    session: AsyncSession,
    booking: Booking,
    seeker: User | None,
    advisor: User | None,
    settings: Settings,
) -> BookingHistoryRead:
    """Consultation History screen — booking summary + notes + document requests."""
    notes_result = await session.execute(booking_note_service.list_for_booking_stmt(booking.id))
    notes = list(notes_result.scalars().all())
    authors: dict[uuid.UUID, User] = {}
    for note in notes:
        if note.author_id not in authors:
            author = await session.get(User, note.author_id)
            if author is not None:
                authors[note.author_id] = author

    docs_result = await session.execute(booking_document_service.list_for_booking_stmt(booking.id))
    docs = list(docs_result.scalars().all())

    return BookingHistoryRead(
        id=booking.id,
        appointment_id=appointment_id_str(booking),
        seeker_id=booking.seeker_id,
        advisor_id=booking.advisor_id,
        seeker_name=seeker.full_name if seeker else None,
        seeker_email=seeker.email if seeker else None,
        advisor_name=advisor.full_name if advisor else None,
        service_type=booking.service_type,
        scheduled_start=as_utc(booking.scheduled_start),
        scheduled_end=as_utc(booking.scheduled_end),
        status=booking.status,
        seeker_note=booking.seeker_note,
        is_important=booking.is_important,
        deal_later_at=as_utc(booking.deal_later_at) if booking.deal_later_at else None,
        notes=[
            booking_note_service.build_read(n, authors.get(n.author_id), settings) for n in notes
        ],
        document_requests=[booking_document_service.build_read(d, settings) for d in docs],
        created_at=booking.created_at,
    )


def _file_format(file_name: str | None, content_type: str | None) -> str:
    if file_name and "." in file_name:
        return file_name.rsplit(".", 1)[-1].upper()
    if content_type and "/" in content_type:
        return content_type.rsplit("/", 1)[-1].upper()
    return "FILE"


def _meeting_read(booking: Booking) -> BookingMeetingRead:
    start = as_utc(booking.scheduled_start)
    end = as_utc(booking.scheduled_end)
    label = humanize_slug(booking.service_type) or "Consultation"
    return BookingMeetingRead(
        label=label,
        time_range=(
            f"{start.strftime('%I:%M %p').lstrip('0')} - "
            f"{end.strftime('%I:%M %p').lstrip('0')} UTC"
        ),
        date=start.strftime("%d %b %Y"),
    )


async def build_details(
    session: AsyncSession,
    booking: Booking,
    seeker: User | None,
) -> BookingDetailsRead:
    """View Booking Details drawer — payment, attachments, meeting, AI suggestions."""
    txn = (
        await session.execute(select(Transaction).where(Transaction.booking_id == booking.id))
    ).scalar_one_or_none()
    if txn is not None:
        amount_paid = round(float(txn.amount_usd), 2)
    elif booking.payment_status == PaymentStatus.paid:
        amount_paid = round(float(booking.price_usd), 2)
    else:
        amount_paid = 0.0

    description = (booking.seeker_note or "").strip() or (
        f"Consultation for {booking.service_type.replace('_', ' ').strip()}"
    )

    attachments: list[BookingAttachmentRead] = []
    docs_result = await session.execute(booking_document_service.list_for_booking_stmt(booking.id))
    for doc in docs_result.scalars().all():
        if doc.status != DocumentRequestStatus.fulfilled or not doc.file_name:
            continue
        attachments.append(
            BookingAttachmentRead(
                id=doc.id,
                title=doc.file_name,
                format=_file_format(doc.file_name, doc.content_type),
                size=int(doc.file_size or 0),
            )
        )

    notes_result = await session.execute(booking_note_service.list_for_booking_stmt(booking.id))
    for note in notes_result.scalars().all():
        for att in note.attachments or []:
            attachments.append(
                BookingAttachmentRead(
                    id=att.id,
                    title=att.file_name,
                    format=_file_format(att.file_name, att.content_type),
                    size=int(att.file_size),
                )
            )

    # AI suggestions are not persisted yet — return an empty list until the
    # suggestion pipeline lands; schema is ready for {id, message} rows.
    ai_suggestions: list[BookingAiSuggestionRead] = []

    return BookingDetailsRead(
        appointment_id=f"#{booking.appointment_number:07d}",
        seeker_name=seeker.full_name if seeker else None,
        service_type=booking.service_type,
        scheduled_start=as_utc(booking.scheduled_start),
        duration_minutes=booking.duration_minutes,
        amount_paid=amount_paid,
        description=description,
        attachments=attachments,
        meeting=_meeting_read(booking),
        ai_suggestions=ai_suggestions,
    )


def list_clients_stmt(
    advisor_id: uuid.UUID,
    q: str | None = None,
    *,
    service_types: list[str] | None = None,
    status: BookingStatus | None = None,
) -> Select[tuple[User]]:
    """Distinct seekers with at least one prior booking with this advisor.

    Powers the calendar's "Select Client" / "Search Client" picker and the
    Clients table — deliberately scoped to existing clients only, not an open
    search across every seeker.

    When ``service_types`` / ``status`` are set, filter on the seeker's
    *latest* booking (max ``scheduled_start``) with this advisor.
    """
    latest_starts = (
        select(
            Booking.seeker_id.label("seeker_id"),
            func.max(Booking.scheduled_start).label("max_start"),
        )
        .where(Booking.advisor_id == advisor_id)
        .group_by(Booking.seeker_id)
    ).subquery()

    stmt = (
        select(User)
        .join(Booking, Booking.seeker_id == User.id)
        .join(
            latest_starts,
            and_(
                Booking.seeker_id == latest_starts.c.seeker_id,
                Booking.scheduled_start == latest_starts.c.max_start,
                Booking.advisor_id == advisor_id,
            ),
        )
        .distinct()
        .order_by(User.full_name)
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(pattern), User.email.ilike(pattern)))
    if service_types:
        stmt = stmt.where(Booking.service_type.in_(service_types))
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    return stmt


async def build_client_reads(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    clients: list[User],
    settings: Settings,
) -> list[ClientRead]:
    """Enrich client picker rows with latest booking + optional lead match_score."""
    if not clients:
        return []

    seeker_ids = [c.id for c in clients]

    bookings = (
        (
            await session.execute(
                select(Booking)
                .where(Booking.advisor_id == advisor_id)
                .where(Booking.seeker_id.in_(seeker_ids))
                .order_by(Booking.scheduled_start.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_booking: dict[uuid.UUID, Booking] = {}
    for booking in bookings:
        if booking.seeker_id not in latest_booking:
            latest_booking[booking.seeker_id] = booking

    leads = (
        (
            await session.execute(
                select(AdvisorLead)
                .where(AdvisorLead.advisor_id == advisor_id)
                .where(AdvisorLead.seeker_id.in_(seeker_ids))
                .where(AdvisorLead.status != AdvisorLeadStatus.dismissed)
                .order_by(AdvisorLead.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_lead: dict[uuid.UUID, AdvisorLead] = {}
    for lead in leads:
        if lead.seeker_id not in latest_lead:
            latest_lead[lead.seeker_id] = lead

    photos = await seeker_photo_keys(session, set(seeker_ids))

    rows: list[ClientRead] = []
    for client in clients:
        latest = latest_booking.get(client.id)
        lead_row = latest_lead.get(client.id)
        appt = appointment_id_str(latest) if latest is not None else None
        rows.append(
            ClientRead(
                id=client.id,
                seeker_id=client.id,
                full_name=client.full_name,
                email=client.email,
                seeker_profile_photo_url=resolve_media_url(photos.get(client.id), settings),
                booking_id=latest.id if latest is not None else None,
                consultation_number=appt,
                appointment_id=appt,
                consultation_type=latest.service_type if latest is not None else None,
                match_score=lead_row.match_score if lead_row is not None else None,
                status=latest.status if latest is not None else None,
                is_important=latest.is_important if latest is not None else False,
            )
        )
    return rows


async def has_client_relationship(
    session: AsyncSession, advisor_id: uuid.UUID, seeker_id: uuid.UUID
) -> bool:
    """Whether the advisor has ever had a booking with this seeker.

    Used to gate "assigned advisor" access to a seeker's data (e.g. their
    document portfolio) that isn't itself booking-scoped.
    """
    result = await session.execute(
        select(Booking.id)
        .where(Booking.advisor_id == advisor_id)
        .where(Booking.seeker_id == seeker_id)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None
