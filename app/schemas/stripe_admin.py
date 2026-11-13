"""Admin-facing Stripe status and webhook ledger schemas (EPIC 04, PAY-108 / PAY-115)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class StripeStatusRead(BaseModel):
    """Never contains key material: presence flags and probe results only."""

    mode: Literal["test", "live", "unset"]
    checks: dict[str, bool]
    ok: bool
    live_payments_enabled: bool


class WebhookEventRead(BaseModel):
    event_id: str
    event_type: str
    status: Literal["received", "processed", "failed", "ignored"]
    attempts: int
    error: str | None
    received_at: datetime
    processed_at: datetime | None
