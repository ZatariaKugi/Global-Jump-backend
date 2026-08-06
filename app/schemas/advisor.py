"""Advisor-specific request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import validate_password_strength
from app.models.user import SignupSource, VerificationStatus
from app.schemas.user import UserRead


class AdvisorCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    signup_source: SignupSource | None = None
    # role is hardcoded to advisor server-side

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, v: str) -> str:
        return validate_password_strength(v)


class AdvisorRead(UserRead):
    model_config = ConfigDict(from_attributes=True)

    verification_status: VerificationStatus | None
