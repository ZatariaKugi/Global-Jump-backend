"""Booking lifecycle — instant booking, cancellation policy, state transitions."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from app.models.advisor_profile import AdvisorProfile, AdvisorService
from app.models.booking import Booking, BookingStatus
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.booking import AdvisorBookingCreate, BookingCreate
from app.services import availability_service
from app.services.availability_service import as_utc

DEFAULT_NOTICE_HOURS = 24


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
    service = await _resolve_service(session, data.advisor_id, data.service_type)

    start_utc = as_utc(data.scheduled_start)
    if start_utc <= datetime.now(UTC):
        raise AppError("Booking must be in the future", code="invalid_booking")

    end_utc = await _assert_slot_free(session, data.advisor_id, start_utc, service.duration_minutes)

    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=data.advisor_id,
        service_type=service.service_type,
        duration_minutes=service.duration_minutes,
        price_usd=service.price_usd,
        scheduled_start=start_utc,
        scheduled_end=end_utc,
        status=BookingStatus.pending,  # advisor must accept/reject before it's confirmed
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

    service = await _resolve_service(session, advisor.id, data.service_type)

    start_utc = as_utc(data.scheduled_start)
    if start_utc <= datetime.now(UTC):
        raise AppError("Booking must be in the future", code="invalid_booking")

    end_utc = await _assert_slot_free(session, advisor.id, start_utc, service.duration_minutes)

    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor.id,
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
) -> Select[tuple[Booking]]:
    column = Booking.advisor_id if role == UserRole.advisor else Booking.seeker_id
    stmt = select(Booking).where(column == user_id).order_by(Booking.scheduled_start.desc())
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
    return stmt


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


def list_clients_stmt(advisor_id: uuid.UUID, q: str | None = None) -> Select[tuple[User]]:
    """Distinct seekers with at least one prior booking with this advisor.

    Powers the calendar's "Select Client" / "Search Client" picker — deliberately
    scoped to existing clients only, not an open search across every seeker.
    """
    stmt = (
        select(User)
        .join(Booking, Booking.seeker_id == User.id)
        .where(Booking.advisor_id == advisor_id)
        .distinct()
        .order_by(User.full_name)
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(pattern), User.email.ilike(pattern)))
    return stmt


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
