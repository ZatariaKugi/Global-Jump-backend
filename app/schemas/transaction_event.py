"""Schemas for the transaction timeline/logs (PRD §4.5 Finance Management)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.transaction_event import TransactionEventType


class TransactionEventRead(BaseModel):
    model_config = {"from_attributes": True}

    event_type: TransactionEventType
    occurred_at: datetime
