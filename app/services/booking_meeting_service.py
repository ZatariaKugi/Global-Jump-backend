"""Provision and lifecycle-manage Zoom meetings for paid, confirmed bookings."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.visa_types import humanize_slug
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.notification import NotificationEntityType, NotificationType
from app.models.user import User
from app.services import (
    email_service,
    notification_service,
    zoom_connection_service,
    zoom_meeting_service,
)
from app.services.availability_service import as_utc

logger = get_logger(__name__)


def is_eligible_for_meeting(booking: Booking) -> bool:
    """Paid + confirmed seeker bookings get a Zoom meeting."""
    return (
        booking.status == BookingStatus.confirmed
        and booking.payment_status == PaymentStatus.paid
        and booking.price_usd > 0
    )


async def maybe_provision_meeting(
    session: AsyncSession,
    booking: Booking,
    settings: Settings,
) -> bool:
    """Create a Zoom meeting when paid + confirmed. Idempotent; never raises."""
    if not is_eligible_for_meeting(booking):
        return False
    if booking.meeting_id:
        return True
    if not settings.zoom_oauth_enabled:
        logger.warning("zoom_meeting_skipped_not_configured", booking_id=str(booking.id))
        return False

    try:
        access_token = await zoom_connection_service.get_valid_zoom_access_token(
            session, booking.advisor_id, settings
        )
    except AppError as exc:
        logger.warning(
            "zoom_meeting_skipped_no_token",
            booking_id=str(booking.id),
            code=exc.code,
        )
        await _notify_provision_failed(session, booking)
        return False

    seeker = await session.get(User, booking.seeker_id)
    advisor = await session.get(User, booking.advisor_id)
    service_label = humanize_slug(booking.service_type) or booking.service_type
    topic = f"{settings.EMAILS_FROM_NAME}: {service_label}"
    if seeker and seeker.full_name:
        topic = f"{topic} with {seeker.full_name}"

    try:
        meeting = await zoom_meeting_service.create_scheduled_meeting(
            access_token,
            topic=topic,
            start_utc=as_utc(booking.scheduled_start),
            duration_minutes=booking.duration_minutes,
        )
    except AppError as exc:
        logger.warning(
            "zoom_meeting_provision_failed",
            booking_id=str(booking.id),
            code=exc.code,
        )
        await _notify_provision_failed(session, booking)
        return False

    booking.meeting_platform = "zoom"
    booking.meeting_id = meeting.meeting_id
    booking.meeting_passcode = meeting.passcode
    booking.meeting_join_url = meeting.join_url
    booking.meeting_start_url = meeting.start_url
    session.add(booking)
    await session.flush()
    await session.refresh(booking)

    await _send_meeting_notifications(session, booking, seeker, advisor, settings)
    logger.info(
        "zoom_meeting_provisioned",
        booking_id=str(booking.id),
        meeting_id=meeting.meeting_id,
    )
    return True


async def sync_meeting_on_reschedule(
    session: AsyncSession,
    booking: Booking,
    settings: Settings,
) -> None:
    """Update Zoom meeting time after a booking reschedule. Best-effort."""
    if not booking.meeting_id or not settings.zoom_oauth_enabled:
        return
    try:
        access_token = await zoom_connection_service.get_valid_zoom_access_token(
            session, booking.advisor_id, settings
        )
        await zoom_meeting_service.update_scheduled_meeting(
            access_token,
            booking.meeting_id,
            start_utc=as_utc(booking.scheduled_start),
            duration_minutes=booking.duration_minutes,
        )
        logger.info("zoom_meeting_rescheduled", booking_id=str(booking.id))
    except AppError as exc:
        logger.warning(
            "zoom_meeting_reschedule_failed",
            booking_id=str(booking.id),
            code=exc.code,
        )


async def remove_meeting(
    session: AsyncSession,
    booking: Booking,
    settings: Settings,
) -> None:
    """Delete Zoom meeting and clear local meeting fields. Best-effort."""
    meeting_id = booking.meeting_id
    if not meeting_id or not settings.zoom_oauth_enabled:
        _clear_meeting_fields(booking)
        session.add(booking)
        await session.flush()
        return

    try:
        access_token = await zoom_connection_service.get_valid_zoom_access_token(
            session, booking.advisor_id, settings
        )
        await zoom_meeting_service.delete_meeting(access_token, meeting_id)
        logger.info("zoom_meeting_deleted", booking_id=str(booking.id), meeting_id=meeting_id)
    except AppError as exc:
        logger.warning(
            "zoom_meeting_delete_failed",
            booking_id=str(booking.id),
            meeting_id=meeting_id,
            code=exc.code,
        )

    _clear_meeting_fields(booking)
    session.add(booking)
    await session.flush()


def _clear_meeting_fields(booking: Booking) -> None:
    booking.meeting_platform = None
    booking.meeting_id = None
    booking.meeting_passcode = None
    booking.meeting_join_url = None
    booking.meeting_start_url = None


async def _notify_provision_failed(session: AsyncSession, booking: Booking) -> None:
    await notification_service.notify(
        session,
        user_id=booking.advisor_id,
        type=NotificationType.booking_confirmed,
        title="Zoom meeting could not be created",
        body=(
            "Payment was received but we could not schedule the video call. "
            "Please reconnect Zoom from your profile and contact support if this persists."
        ),
        entity_type=NotificationEntityType.booking,
        entity_id=booking.id,
        actor_id=booking.seeker_id,
    )


async def _send_meeting_notifications(
    session: AsyncSession,
    booking: Booking,
    seeker: User | None,
    advisor: User | None,
    settings: Settings,
) -> None:
    if seeker is not None and booking.meeting_join_url:
        email_service.schedule_email(
            email_service.send_booking_meeting_email(
                seeker.email,
                seeker.full_name or seeker.email,
                other_party=advisor.full_name if advisor and advisor.full_name else "Advisor",
                service_type=booking.service_type,
                start_utc=as_utc(booking.scheduled_start),
                duration_minutes=booking.duration_minutes,
                meeting_url=booking.meeting_join_url,
                passcode=booking.meeting_passcode,
                is_host=False,
                settings=settings,
            )
        )
        service_label = humanize_slug(booking.service_type) or "consultation"
        await notification_service.notify(
            session,
            user_id=seeker.id,
            type=NotificationType.booking_confirmed,
            title="Your video meeting link is ready",
            body=f"Join your {service_label} via the link in your email.",
            entity_type=NotificationEntityType.booking,
            entity_id=booking.id,
            actor_id=booking.advisor_id,
        )

    if advisor is not None and booking.meeting_start_url:
        email_service.schedule_email(
            email_service.send_booking_meeting_email(
                advisor.email,
                advisor.full_name or advisor.email,
                other_party=seeker.full_name if seeker and seeker.full_name else "Client",
                service_type=booking.service_type,
                start_utc=as_utc(booking.scheduled_start),
                duration_minutes=booking.duration_minutes,
                meeting_url=booking.meeting_start_url,
                passcode=booking.meeting_passcode,
                is_host=True,
                settings=settings,
            )
        )
        client_name = seeker.full_name if seeker else "client"
        await notification_service.notify(
            session,
            user_id=advisor.id,
            type=NotificationType.booking_confirmed,
            title="Video meeting scheduled",
            body=f"Host link sent for your session with {client_name}.",
            entity_type=NotificationEntityType.booking,
            entity_id=booking.id,
            actor_id=booking.seeker_id,
        )
