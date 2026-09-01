"""Schemas for the public support / contact-us endpoint."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class SupportMessageCreate(BaseModel):
    """Public contact-us payload — no auth required."""

    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    message: str = Field(min_length=1, max_length=5000)


class SupportMessageRead(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    message: str
    created_at: datetime
