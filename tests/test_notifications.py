"""Notification outbox & FCM push-service tests (PR #82 review follow-ups).

These exercise the service layer directly against the in-memory SQLite session so the
outbox state machine is tested without a live Firebase or the full HTTP stack. Firebase
itself is stubbed: ``firebase_admin._apps`` is patched to look initialised and the
blocking ``messaging.send_each_for_multicast`` call is replaced with a fake.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_settings
from app.models.device_token import DevicePlatform, DeviceToken
from app.models.notification import Notification, NotificationType, PushStatus
from app.models.user import User, UserRole
from app.services import notification_service, push_service


async def _user(session: AsyncSession) -> User:
    user = User(
        email=f"{uuid.uuid4().hex}@test.com",
        full_name="Recipient",
        role=UserRole.seeker,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def _pending(session: AsyncSession, user_id: uuid.UUID, **kw) -> Notification:
    n = await notification_service.notify(
        session,
        user_id=user_id,
        type=NotificationType.message_received,
        title="hi",
        body="there",
        **kw,
    )
    # SQLite fills created_at from the server_default as a *naive* datetime, which the
    # sweep's timezone-aware stale-cutoff comparison can't order against (Postgres
    # stores it tz-aware, so this only bites the test DB). Stamp it aware here.
    n.created_at = datetime.now(UTC)
    await session.flush()
    return n


def _fake_batch(successes: list[bool], exceptions: list | None = None):
    exceptions = exceptions or [None] * len(successes)
    responses = [
        SimpleNamespace(success=s, exception=e)
        for s, e in zip(successes, exceptions, strict=True)
    ]
    return SimpleNamespace(responses=responses)


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


# --- Critical: enabled-but-uninitialised must NOT mass-skip -------------------


async def test_enabled_but_uninitialised_leaves_rows_pending(session, settings, monkeypatch):
    """When push is enabled but Firebase init fails, pending rows stay pending."""
    user = await _user(session)
    n = await _pending(session, user.id)
    await session.commit()

    monkeypatch.setattr(settings, "FIREBASE_CREDENTIALS_FILE", "/nonexistent.json")
    # init fails (bad path) and there is no initialised app.
    with mock.patch.object(push_service.firebase_admin, "_apps", {}):
        sent = await push_service.run_due_pushes(session, settings)
    await session.commit()

    assert sent == 0
    await session.refresh(n)
    assert n.push_status == PushStatus.pending  # NOT skipped


async def test_push_disabled_drains_to_skipped(session, settings, monkeypatch):
    """With push intentionally off (no credentials), pending rows drain to skipped."""
    user = await _user(session)
    n = await _pending(session, user.id)
    await session.commit()

    monkeypatch.setattr(settings, "FIREBASE_CREDENTIALS_FILE", None)
    sent = await push_service.run_due_pushes(session, settings)
    await session.commit()

    assert sent == 0
    await session.refresh(n)
    assert n.push_status == PushStatus.skipped


# --- Major: no device tokens defers instead of skipping -----------------------


async def test_no_device_tokens_defers_not_skips(session, settings, monkeypatch):
    user = await _user(session)
    n = await _pending(session, user.id)  # user has no registered device
    await session.commit()

    monkeypatch.setattr(settings, "FIREBASE_CREDENTIALS_FILE", "/creds.json")
    with mock.patch.object(push_service.firebase_admin, "_apps", {"[DEFAULT]": object()}):
        sent = await push_service.run_due_pushes(session, settings)
    await session.commit()

    assert sent == 0
    await session.refresh(n)
    assert n.push_status == PushStatus.pending  # deferred, not skipped
    assert n.push_attempts == 1
    assert n.push_next_attempt_at is not None  # backoff armed


# --- Delivery happy path + dead-token pruning ---------------------------------


async def test_successful_dispatch_marks_sent(session, settings, monkeypatch):
    user = await _user(session)
    await notification_service.register_device(
        session, user.id, "tok-good", DevicePlatform.android
    )
    n = await _pending(session, user.id)
    await session.commit()

    monkeypatch.setattr(settings, "FIREBASE_CREDENTIALS_FILE", "/creds.json")
    with (
        mock.patch.object(push_service.firebase_admin, "_apps", {"[DEFAULT]": object()}),
        mock.patch.object(
            push_service.messaging,
            "send_each_for_multicast",
            return_value=_fake_batch([True]),
        ),
    ):
        sent = await push_service.run_due_pushes(session, settings)
    await session.commit()

    assert sent == 1
    await session.refresh(n)
    assert n.push_status == PushStatus.sent


async def test_unregistered_token_is_pruned(session, settings, monkeypatch):
    from firebase_admin import messaging as fb_messaging

    user = await _user(session)
    await notification_service.register_device(
        session, user.id, "tok-dead", DevicePlatform.ios
    )
    await _pending(session, user.id)
    await session.commit()

    monkeypatch.setattr(settings, "FIREBASE_CREDENTIALS_FILE", "/creds.json")
    dead = fb_messaging.UnregisteredError("gone")
    with (
        mock.patch.object(push_service.firebase_admin, "_apps", {"[DEFAULT]": object()}),
        mock.patch.object(
            push_service.messaging,
            "send_each_for_multicast",
            return_value=_fake_batch([False], [dead]),
        ),
    ):
        await push_service.run_due_pushes(session, settings)
    await session.commit()

    remaining = await notification_service.tokens_for_users(session, [user.id])
    assert remaining.get(user.id, []) == []  # dead token pruned


# --- mark_read cancels a pending push -----------------------------------------


async def test_mark_read_skips_pending_push(session):
    user = await _user(session)
    n = await _pending(session, user.id)
    await session.commit()

    await notification_service.mark_read(session, user.id, n.id)
    await session.commit()

    await session.refresh(n)
    assert n.read_at is not None
    assert n.push_status == PushStatus.skipped


# --- Minor: device registration cap evicts oldest -----------------------------


async def test_device_cap_evicts_least_recently_seen(session):
    user = await _user(session)
    # Register 3 tokens with increasing last_seen; cap at 2 should keep the newest 2.
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(3):
        dev = DeviceToken(
            user_id=user.id,
            token=f"tok-{i}",
            platform=DevicePlatform.web,
            last_seen_at=base + timedelta(minutes=i),
        )
        session.add(dev)
    await session.flush()

    # New registration with cap=2 evicts down to the newest (cap-1) + the new one.
    await notification_service.register_device(
        session, user.id, "tok-new", DevicePlatform.web, max_devices=2
    )
    await session.commit()

    tokens = {
        d.token
        for d in (await notification_service.tokens_for_users(session, [user.id])).get(
            user.id, []
        )
    }
    assert tokens == {"tok-2", "tok-new"}  # oldest (tok-0, tok-1) evicted
    assert len(tokens) == 2
