"""Advisor dashboard aggregate schemas (FE ``/advisor/dashboard``).

A single home-screen summary for an advisor — next appointment, windowed stat
tiles, the regulatory-updates card, and a client-inquiries preview. The optional
``days`` window (7 / 30 / 90 / 180 / 365; omitted = all-time) drives windowed
``stats.*`` figures and the regulatory card; identity/profile-level figures like
``profile_completion_percent`` are not windowed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator

from app.schemas.conversation import ConversationRead


def _coerce_window(value: object) -> object:
    """Coerce query-string ``days`` (e.g. ``"30"``) to int before the literal check."""
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


# Overview toolbar windows shared by admin / seeker / advisor. Query params
# arrive as strings, so coerce to int before validating against the literal set.
DashboardWindow = Annotated[Literal[7, 30, 90, 180, 365], BeforeValidator(_coerce_window)]

# Derived from the last message on the thread:
#   new       — no message from either side yet (thread opened, awaiting first msg)
#   unread    — the seeker's latest message is unread by the advisor
#   responded — the advisor sent the most recent message
ClientInquiryStatus = Literal["new", "unread", "responded"]


class NextUpcomingRead(BaseModel):
    """The advisor's soonest pending/confirmed appointment, if any."""

    booking_id: uuid.UUID
    appointment_id: str
    seeker_id: uuid.UUID
    seeker_name: str | None
    service_type: str
    status: str
    scheduled_start: datetime
    scheduled_end: datetime


class DashboardStats(BaseModel):
    """All figures scoped to the selected ``days`` window unless noted."""

    new_leads_count: int  # AI-matched leads still in "new" status, created in-window
    total_earned_usd: float  # advisor payout on succeeded transactions in-window
    pending_reviews_count: int  # public reviews with no advisor_response (all-time)
    profile_completion_percent: int  # 0–100, NOT windowed


class RegulatoryUpdateRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    country_code: str | None
    region_label: str
    title: str
    description: str
    published_at: datetime


class ClientInquiryRead(ConversationRead):
    """A conversation thread rendered as a dashboard inquiry row."""

    status: ClientInquiryStatus


class AdvisorDashboardRead(BaseModel):
    window_days: int | None  # null = all-time (``days`` omitted)
    next_upcoming: NextUpcomingRead | None
    stats: DashboardStats
    regulatory_updates: list[RegulatoryUpdateRead]  # newest first, capped for the card
    client_inquiries: list[ClientInquiryRead]  # most-recent threads, capped for the card
    # Persistent connect banners (NOT windowed by ``days``). FE shows while true.
    needs_stripe_connect: bool
    needs_zoom_connect: bool
