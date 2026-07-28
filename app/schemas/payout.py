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
    # Bank fields are optional even for bank_transfer: the FE Request Payout modal
    # sends amount / method / note only, deferring account collection to Stripe
    # Connect onboarding. When supplied they're persisted; when absent the request
    # is created note-only rather than 422-ing.
    account_holder_name: str | None = Field(default=None, max_length=255)
    account_number: str | None = Field(default=None, max_length=64)
    bank_name: str | None = Field(default=None, max_length=255)
    swift_code: str | None = Field(default=None, max_length=32)


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
    account_holder_name: str | None = None
    account_number: str | None = None
    bank_name: str | None = None
    swift_code: str | None = None
    processing_fee_usd: float
    net_amount_usd: float
    status: PayoutStatus
    processed_at: datetime | None
    rejection_reason: str | None
    created_at: datetime


class PayoutPreviewRead(BaseModel):
    available_balance_usd: float
    amount_usd: float
    processing_fee_usd: float
    processing_fee_rate: float
    net_amount_usd: float
