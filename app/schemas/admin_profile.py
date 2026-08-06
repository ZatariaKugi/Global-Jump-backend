"""Admin profile schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AdminProfileUpdate(BaseModel):
    """PATCH /admins/me/profile — all fields optional."""

    title: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=50)
    country_of_residence: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str | None = Field(default=None, max_length=64)
    preferred_language: str | None = Field(default=None, max_length=10)
    about: str | None = None
    profile_photo_url: str | None = None
    banner_url: str | None = None


class AdminProfileRead(BaseModel):
    """GET /admins/me/profile response."""

    id: uuid.UUID
    user_id: uuid.UUID
    full_name: str | None = None
    email: str
    title: str | None = None
    phone: str | None = None
    country_of_residence: str | None = None
    timezone: str | None = None
    preferred_language: str | None = None
    about: str | None = None
    profile_photo_url: str | None = None
    banner_url: str | None = None
    is_active: bool
    is_email_verified: bool
    created_at: datetime
    updated_at: datetime
