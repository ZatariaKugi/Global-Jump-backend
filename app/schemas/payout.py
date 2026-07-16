"""Schemas for advisor payout requests (PRD §3.10)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.payout_request import PayoutMethod, PayoutStatus


class PayoutRequestCreate(BaseModel):
    amount_usd: float = Field(gt=0)
    method: PayoutMethod
    note: str | None = Field(default=None, max_length=1000)
    account_holder_name: str | None = Field(default=None, max_length=255)
    account_number: str | None = Field(default=None, max_length=64)
    bank_name: str | None = Field(default=None, max_length=255)
    swift_code: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _require_bank_fields(self) -> PayoutRequestCreate:
        if self.method == PayoutMethod.bank_transfer:
            missing = [
                name
                for name, value in (
                    ("account_holder_name", self.account_holder_name),
                    ("account_number", self.account_number),
                    ("bank_name", self.bank_name),
                    ("swift_code", self.swift_code),
                )
                if not (value and value.strip())
            ]
            if missing:
                raise ValueError(
                    "Bank transfer requires: " + ", ".join(missing)
                )
        return self


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
