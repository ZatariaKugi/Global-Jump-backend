"""Schemas for advisor payout requests (PRD §3.10)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.payout_request import PayoutMethod, PayoutStatus


class PayoutRequestCreate(BaseModel):
    amount_usd: float = Field(gt=0)
    method: PayoutMethod
    note: str | None = Field(default=None, max_length=1000)


class PayoutDecision(BaseModel):
    action: PayoutStatus  # completed | rejected
    rejection_reason: str | None = Field(default=None, max_length=500)


class PayoutRequestRead(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    advisor_id: uuid.UUID
    amount_usd: float
    method: PayoutMethod
    note: str | None
    processing_fee_usd: float
    net_amount_usd: float
    status: PayoutStatus
    processed_at: datetime | None
    rejection_reason: str | None
    created_at: datetime
