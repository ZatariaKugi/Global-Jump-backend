"""Admin-facing request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from app.models.user import VerificationStatus


class VerificationStatusUpdate(BaseModel):
    status: VerificationStatus
    reason: str | None = None

    @field_validator("status")
    @classmethod
    def reject_suspended(cls, value: VerificationStatus) -> VerificationStatus:
        if value == VerificationStatus.suspended:
            raise ValueError("Use POST /admin/users/{id}/suspend to suspend an advisor")
        return value


class FeatureFlagUpdate(BaseModel):
    is_featured: bool
