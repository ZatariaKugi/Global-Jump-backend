"""Auth token schemas."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole, VerificationStatus


class Token(BaseModel):
    """Bare OAuth2 token — kept for Swagger Authorize compatibility."""

    access_token: str
    token_type: str = "bearer"


class TokenPair(BaseModel):
    """Full token pair returned from login and refresh endpoints.

    ``role`` (and ``verification_status`` for advisors) is included alongside the
    tokens so the frontend can redirect immediately without decoding the JWT.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: UserRole
    verification_status: VerificationStatus | None = None


class TokenPayload(BaseModel):
    """Decoded JWT claims we care about."""

    sub: uuid.UUID
    iss: str | None = None
    role: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class EmailVerifyRequest(BaseModel):
    token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)
