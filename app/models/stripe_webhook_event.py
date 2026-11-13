"""Stripe webhook event ledger (EPIC 04, PAY-106 / PAY-115).

Every delivered event is written here BEFORE its handler runs, keyed by Stripe's
own event id. A redelivery of an already ``processed`` or ``ignored`` event never
reaches a handler again; a ``failed`` event is re-run on redelivery (Stripe retries
when we answer 5xx) with ``attempts`` counting up.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WebhookEventStatus(StrEnum):
    received = "received"  # row written, handler not finished
    processed = "processed"  # handler succeeded
    failed = "failed"  # handler raised; will run again on redelivery
    ignored = "ignored"  # no handler for this event type


class StripeWebhookEvent(Base):
    __tablename__ = "stripe_webhook_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[WebhookEventStatus] = mapped_column(
        SAEnum(WebhookEventStatus, name="webhook_event_status"),
        nullable=False,
        default=WebhookEventStatus.received,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
