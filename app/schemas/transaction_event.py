"""Schemas for the transaction timeline/logs (PRD §4.5 Finance Management)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.transaction_event import TransactionEvent, TransactionEventType

TimelineLogStatus = Literal["success", "pending", "failed"]

# (title, description, source, status)
# title → Logs "Event" bold line / Timeline step label
# description → Logs subtitle
# source → Timeline actor + Logs "Source"
# status → Logs status badge
_EVENT_DISPLAY: dict[TransactionEventType, tuple[str, str, str, TimelineLogStatus]] = {
    TransactionEventType.initiated: (
        "Payment Initiate",
        "Payment process was initiated by the customer",
        "System",
        "success",
    ),
    TransactionEventType.authorized: (
        "Payment Authorized",
        "Payment was authorized by the payment provider",
        "System",
        "success",
    ),
    TransactionEventType.completed: (
        "Payment Completed",
        "Payment was completed successfully",
        "System",
        "success",
    ),
    TransactionEventType.invoice_generated: (
        "Invoice Generated",
        "Invoice was generated for this payment",
        "System",
        "success",
    ),
    TransactionEventType.receipt_sent: (
        "Receipt Sent",
        "Payment receipt was sent to the customer",
        "System",
        "success",
    ),
    TransactionEventType.refunded: (
        "Payment Refunded",
        "Payment was refunded",
        "System",
        "success",
    ),
    TransactionEventType.failed: (
        "Payment Failed",
        "Payment process failed",
        "System",
        "failed",
    ),
    TransactionEventType.closed: (
        "Payment Closed",
        "Payment process was closed",
        "System",
        "success",
    ),
}


class TransactionEventRead(BaseModel):
    """One row for Finance \"Timeline & Logs\" modal (both tabs share this shape)."""

    event_type: TransactionEventType
    occurred_at: datetime
    title: str  # Logs "Event" heading (e.g. Payment Initiate)
    description: str  # Logs subtitle under the heading
    source: str  # Timeline actor and Logs "Source"
    status: TimelineLogStatus  # Logs status badge (success/pending/failed)

    @classmethod
    def build(cls, event: TransactionEvent) -> TransactionEventRead:
        title, description, source, status = _EVENT_DISPLAY[event.event_type]
        return cls(
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            title=title,
            description=description,
            source=source,
            status=status,
        )
