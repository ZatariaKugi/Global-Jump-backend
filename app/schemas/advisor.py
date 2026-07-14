"""Advisor-specific request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import SignupSource, VerificationStatus
from app.schemas.user import UserRead


class AdvisorCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    signup_source: SignupSource | None = None
    # role is hardcoded to advisor server-side


class AdvisorRead(UserRead):
    model_config = ConfigDict(from_attributes=True)

    verification_status: VerificationStatus | None
