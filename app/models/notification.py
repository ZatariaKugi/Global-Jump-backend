"""Notification rows: the in-app feed and the push-delivery outbox in one table.

Rows are written by service code inside the same transaction as the domain change
they announce (transactional outbox) — a rollback removes them, a commit makes the
push an obligation. The scheduler sweep (``push_service.run_due_pushes``) delivers
committed ``pending`` rows to FCM and records the outcome in the ``push_*`` columns.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class NotificationType(StrEnum):
    booking_requested = "booking_requested"
    booking_confirmed = "booking_confirmed"
    booking_rejected = "booking_rejected"
    booking_cancelled = "booking_cancelled"
    booking_rescheduled = "booking_rescheduled"
    booking_completed = "booking_completed"
    booking_no_show = "booking_no_show"
    booking_note_added = "booking_note_added"
    document_requested = "document_requested"
    document_fulfilled = "document_fulfilled"
    payment_succeeded = "payment_succeeded"
    payment_failed = "payment_failed"
    payment_refunded = "payment_refunded"
    transfer_completed = "transfer_completed"
    transfer_failed = "transfer_failed"
    connect_payouts_enabled = "connect_payouts_enabled"
    payout_requested = "payout_requested"
    payout_completed = "payout_completed"
    payout_rejected = "payout_rejected"
    user_registered = "user_registered"
    message_received = "message_received"
    document_comment = "document_comment"


class NotificationEntityType(StrEnum):
    booking = "booking"
    transaction = "transaction"
    payout_request = "payout_request"
    user = "user"
    conversation = "conversation"
    seeker_document = "seeker_document"


class PushStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class Notification(BaseModel):
    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "ix_notifications_push_status_push_next_attempt_at",
            "push_status",
            "push_next_attempt_at",
        ),
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # Stored as a plain string (not a PG enum) so adding event types never needs
    # an ALTER TYPE migration.
    type: Mapped[NotificationType] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    entity_type: Mapped[NotificationEntityType | None] = mapped_column(
        SAEnum(NotificationEntityType, name="notification_entity_type"), nullable=True
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    push_status: Mapped[PushStatus] = mapped_column(
        SAEnum(PushStatus, name="notification_push_status"),
        nullable=False,
        default=PushStatus.pending,
        server_default=PushStatus.pending.value,
    )
    push_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    push_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    push_last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
