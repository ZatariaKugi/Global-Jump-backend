"""Schemas for admin user impersonation."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.models.user import UserRole, VerificationStatus
from app.schemas.user import UserRead


class ImpersonationRead(BaseModel):
    """Token response so the admin frontend can act as the target user.

    No refresh token is issued — impersonation sessions are short-lived access
    tokens only. The frontend should keep the admin's own tokens separately and
    discard this access token when exiting impersonation.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole
    verification_status: VerificationStatus | None = None
    user: UserRead
    impersonated_by: uuid.UUID
