"""FCM delivery: Firebase Admin initialisation and the notification-outbox sweep.

The sweep claims committed ``pending`` rows with ``FOR UPDATE SKIP LOCKED`` (safe to
run from multiple app instances), sends them through FCM off the event loop, and
records the outcome. Delivery is at-least-once — a crash between the FCM call and
the status commit re-sends on the next pass; clients dedupe on
``data.notification_id``. When Firebase isn't configured, pending rows are marked
``skipped`` so the in-app feed keeps working and nothing raises.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import firebase_admin
from firebase_admin import credentials, messaging
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.device_token import DeviceToken
from app.models.notification import Notification, PushStatus
from app.services import notification_service

logger = get_logger(__name__)

_RETRY_BASE_SECONDS = 30
_SWEEP_BATCH_SIZE = 100


def init_firebase(settings: Settings) -> bool:
    """Initialise the Firebase Admin app once. Returns True when push is usable."""
    if not settings.push_enabled:
        logger.info("push_disabled [no FIREBASE_CREDENTIALS_FILE — notifications stay in-app]")
        return False
    if firebase_admin._apps:
        return True
    try:
        cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_FILE)
        firebase_admin.initialize_app(cred)
    except Exception:  # noqa: BLE001 — bad credentials must not crash startup
        logger.exception("firebase_init_failed")
        return False
    logger.info("firebase_initialized")
    return True


def _build_message(notification: Notification, tokens: list[str]) -> messaging.MulticastMessage:
    return messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=notification.title, body=notification.body),
        data={
            "notification_id": str(notification.id),
            "type": str(notification.type),
            "entity_type": str(notification.entity_type) if notification.entity_type else "",
            "entity_id": str(notification.entity_id) if notification.entity_id else "",
        },
        android=messaging.AndroidConfig(collapse_key=str(notification.type)),
    )


async def run_due_pushes(session: AsyncSession, settings: Settings) -> int:
    """One sweep pass over the notification outbox. Flushes; the caller commits."""
    now = datetime.now(UTC)

    if not settings.push_enabled or not firebase_admin._apps:
        await session.execute(
            update(Notification)
            .where(Notification.push_status == PushStatus.pending)
            .values(push_status=PushStatus.skipped)
        )
        return 0

    # Expire stale backlog so a freshly-enabled FCM never blasts old rows.
    stale_cutoff = now - timedelta(hours=settings.NOTIFICATION_PUSH_STALE_HOURS)
    await session.execute(
        update(Notification)
        .where(
            Notification.push_status == PushStatus.pending,
            Notification.created_at < stale_cutoff,
        )
        .values(push_status=PushStatus.skipped)
    )

    result = await session.execute(
        select(Notification)
        .where(
            Notification.push_status == PushStatus.pending,
            or_(
                Notification.push_next_attempt_at.is_(None),
                Notification.push_next_attempt_at <= now,
            ),
        )
        .order_by(Notification.created_at)
        .limit(_SWEEP_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    tokens_by_user = await notification_service.tokens_for_users(
        session, list({n.user_id for n in due})
    )

    sent = 0
    for notification in due:
        try:
            sent += await _dispatch_one(session, notification, tokens_by_user, settings)
        except Exception:  # noqa: BLE001 — one bad row must not poison the batch
            logger.exception("push_dispatch_failed", notification_id=str(notification.id))
            _record_failure(notification, "internal dispatch error", settings)
    await session.flush()
    return sent


async def _dispatch_one(
    session: AsyncSession,
    notification: Notification,
    tokens_by_user: dict[uuid.UUID, list[DeviceToken]],
    settings: Settings,
) -> int:
    devices = tokens_by_user.get(notification.user_id, [])
    if not devices:
        notification.push_status = PushStatus.skipped
        return 0

    message = _build_message(notification, [d.token for d in devices])
    try:
        # firebase-admin is a blocking SDK — keep it off the event loop.
        batch = await asyncio.to_thread(messaging.send_each_for_multicast, message)
    except Exception as exc:  # noqa: BLE001 — transport failure: retry with backoff
        _record_failure(notification, str(exc), settings)
        return 0

    any_success = False
    for device, response in zip(devices, batch.responses, strict=True):
        if response.success:
            any_success = True
            continue
        if isinstance(
            response.exception, messaging.UnregisteredError | messaging.SenderIdMismatchError
        ):
            # Token is dead (app uninstalled / wrong project) — prune it.
            await session.delete(device)

    if any_success:
        notification.push_status = PushStatus.sent
        return 1
    _record_failure(notification, "all device tokens rejected", settings)
    return 0


def _record_failure(notification: Notification, error: str, settings: Settings) -> None:
    notification.push_attempts += 1
    notification.push_last_error = error[:500]
    if notification.push_attempts >= settings.NOTIFICATION_PUSH_MAX_ATTEMPTS:
        notification.push_status = PushStatus.failed
        return
    backoff = _RETRY_BASE_SECONDS * 2**notification.push_attempts
    notification.push_next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff)
