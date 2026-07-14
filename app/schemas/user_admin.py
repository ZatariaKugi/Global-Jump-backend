"""Admin "User Management" schemas — broad, all-roles account directory."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app.models.user import UserRole, VerificationStatus


class AccountStatus(StrEnum):
    verified = "verified"
    unverified = "unverified"
    suspended = "suspended"


class UserListRead(BaseModel):
    id: uuid.UUID
    full_name: str | None
    email: str
    role: UserRole
    country_of_residence: str | None  # seeker-only; None for advisors/others
    status: AccountStatus
    created_at: datetime


class UserDetailRead(UserListRead):
    verification_status: VerificationStatus | None  # advisor-only; None for seekers
