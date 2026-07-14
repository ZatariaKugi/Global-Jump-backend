"""Admin-facing request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.user import VerificationStatus


class VerificationStatusUpdate(BaseModel):
    status: VerificationStatus
    reason: str | None = None


class FeatureFlagUpdate(BaseModel):
    is_featured: bool
