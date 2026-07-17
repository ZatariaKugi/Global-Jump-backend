"""User request/response schemas (kept separate from the ORM model)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import SignupSource, UserRole


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = None


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)
    signup_source: SignupSource | None = None
    # role is intentionally NOT exposed — public registration always creates seeker


class UserUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserRead(BaseModel):
    """Response schema — ``email`` is ``str`` so reserved TLDs (e.g. ``.test``
    seed accounts) stored in the DB can be returned without EmailStr rejection.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None = None
    role: UserRole
    is_active: bool
    is_email_verified: bool
    created_at: datetime
    updated_at: datetime
