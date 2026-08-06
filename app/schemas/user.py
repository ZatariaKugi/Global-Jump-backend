"""User request/response schemas (kept separate from the ORM model)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import validate_password_strength
from app.models.user import SignupSource, UserRole


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = None


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)
    signup_source: SignupSource | None = None
    # role is intentionally NOT exposed — public registration always creates seeker

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, v: str) -> str:
        return validate_password_strength(v)


class UserUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserRead(BaseModel):
    """Response schema — ``email`` is ``str`` so reserved TLDs (e.g. ``.test``
    seed accounts) stored in the DB can be returned without EmailStr rejection.

    ``profile_photo_url`` is the shared sidebar/session avatar for seeker and
    advisor (resolved media URL). Admins have no profile photo → ``null``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None = None
    role: UserRole
    is_active: bool
    is_email_verified: bool
    profile_photo_url: str | None = None
    created_at: datetime
    updated_at: datetime
