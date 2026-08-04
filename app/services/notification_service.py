"""Notification outbox writes, the in-app feed, and FCM device-token bookkeeping.

``notify()`` is the single write path: it adds a ``Notification`` row to the caller's
session and flushes — never commits — so the row is atomic with the domain change
that triggered it (transactional outbox). Delivery happens later in
``push_service.run_due_pushes``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.device_token import DevicePlatform, DeviceToken
from app.models.notification import (
    Notification,
    NotificationEntityType,
    NotificationType,
    PushStatus,
)
from app.models.user import User, UserRole


async def notify(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    type: NotificationType,
    title: str,
    body: str,
    entity_type: NotificationEntityType | None = None,
    entity_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> Notification:
    """Queue a notification for ``user_id`` inside the current transaction."""
    notification = Notification(
        user_id=user_id,
        actor_id=actor_id,
        type=type,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    session.add(notification)
    await session.flush()
    return notification


async def notify_admins(
    session: AsyncSession,
    *,
    type: NotificationType,
    title: str,
    body: str,
    entity_type: NotificationEntityType | None = None,
    entity_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> list[Notification]:
    """Queue one notification per active, non-suspended admin."""
    result = await session.execute(
        select(User.id).where(
            User.role == UserRole.admin,
            User.is_active.is_(True),
            User.is_suspended.is_(False),
        )
    )
    return [
        await notify(
            session,
            user_id=admin_id,
            type=type,
            title=title,
            body=body,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor_id,
        )
        for admin_id in result.scalars().all()
    ]


# Feed -----------------------------------------------------------------------


def list_for_user_stmt(
    user_id: uuid.UUID, *, unread_only: bool = False
) -> Select[tuple[Notification]]:
    stmt = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return stmt.order_by(Notification.created_at.desc())


async def unread_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return result or 0


async def mark_read(
    session: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification:
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != user_id:
        raise NotFoundError("Notification not found")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        # Already seen in-app — don't also push it later.
        if notification.push_status == PushStatus.pending:
            notification.push_status = PushStatus.skipped
        await session.flush()
    return notification


async def mark_all_read(session: AsyncSession, user_id: uuid.UUID) -> int:
    # Skip pushes for the still-unread rows first, then stamp them read.
    await session.execute(
        update(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            Notification.push_status == PushStatus.pending,
        )
        .values(push_status=PushStatus.skipped)
    )
    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    return cast(CursorResult[Any], result).rowcount or 0


# Device tokens ---------------------------------------------------------------


async def register_device(
    session: AsyncSession,
    user_id: uuid.UUID,
    token: str,
    platform: DevicePlatform,
    *,
    max_devices: int | None = None,
) -> DeviceToken:
    """Upsert by unique token — a token seen under a new account changes owner.

    When ``max_devices`` is set, registering a *new* token beyond the cap evicts the
    user's least-recently-seen tokens first, so a single account can't grow the
    device table without bound.
    """
    existing = await session.scalar(select(DeviceToken).where(DeviceToken.token == token))
    now = datetime.now(UTC)
    if existing is not None:
        existing.user_id = user_id
        existing.platform = platform
        existing.last_seen_at = now
        await session.flush()
        return existing

    if max_devices is not None and max_devices > 0:
        await _evict_stale_devices(session, user_id, keep=max_devices - 1)

    device = DeviceToken(user_id=user_id, token=token, platform=platform, last_seen_at=now)
    session.add(device)
    await session.flush()
    return device


async def _evict_stale_devices(
    session: AsyncSession, user_id: uuid.UUID, *, keep: int
) -> None:
    """Delete all but the ``keep`` most-recently-seen tokens for ``user_id``."""
    result = await session.execute(
        select(DeviceToken)
        .where(DeviceToken.user_id == user_id)
        .order_by(DeviceToken.last_seen_at.desc())
    )
    devices = list(result.scalars().all())
    for device in devices[keep:]:
        await session.delete(device)
    if len(devices) > keep:
        await session.flush()


async def unregister_device(session: AsyncSession, user_id: uuid.UUID, token: str) -> None:
    device = await session.scalar(
        select(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user_id)
    )
    if device is not None:
        await session.delete(device)
        await session.flush()


async def tokens_for_users(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[DeviceToken]]:
    if not user_ids:
        return {}
    result = await session.execute(select(DeviceToken).where(DeviceToken.user_id.in_(user_ids)))
    grouped: dict[uuid.UUID, list[DeviceToken]] = {}
    for device in result.scalars().all():
        grouped.setdefault(device.user_id, []).append(device)
    return grouped
