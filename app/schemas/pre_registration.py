"""Pre-registration (lead) request/response schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.pre_registration import PreRegistrationInterest

_PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9 ()-]{5,19}$")


class PreRegistrationCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(min_length=1, max_length=30)
    country: str = Field(min_length=2, max_length=2)
    city: str = Field(min_length=1, max_length=100)
    interest: PreRegistrationInterest
    message: str = Field(default="", max_length=2000)

    @field_validator("full_name", "city", "message", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        value = value.strip()
        if not _PHONE_PATTERN.match(value):
            raise ValueError("Enter a valid phone number")
        return value

    @field_validator("country")
    @classmethod
    def _country(cls, value: str) -> str:
        value = value.strip().upper()
        if not (len(value) == 2 and value.isalpha()):
            raise ValueError("Country must be an ISO 3166-1 alpha-2 code")
        return value


class PreRegistrationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: str
    phone: str
    country: str
    city: str
    interest: PreRegistrationInterest
    message: str
    created_at: datetime
