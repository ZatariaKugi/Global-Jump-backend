"""Local user account model (for users this service authenticates directly)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class UserRole(StrEnum):
    seeker = "seeker"
    advisor = "advisor"
    admin = "admin"


class VerificationStatus(StrEnum):
    pending = "pending"
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    # Soft-suspend badge for the admin UI (frontend reads ``verification_status``).
    suspended = "suspended"


class SignupSource(StrEnum):
    organic = "organic"
    paid_ads = "paid_ads"
    referral_program = "referral_program"
    social_media = "social_media"
    other = "other"


class User(BaseModel):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
    # Explicit admin soft-suspend — distinct from advisor onboarding (is_active=False + pending).
    is_suspended: Mapped[bool] = mapped_column(
        default=False, server_default="false", nullable=False, sort_order=102
    )
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role"),
        default=UserRole.seeker,
        server_default=UserRole.seeker.value,
        nullable=False,
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, sort_order=103
    )
    verification_status: Mapped[VerificationStatus | None] = mapped_column(
        SAEnum(VerificationStatus, name="verification_status"),
        nullable=True,
        sort_order=104,
    )
    # Restored by /reactivate after /suspend overwrites ``verification_status``.
    pre_suspend_verification_status: Mapped[VerificationStatus | None] = mapped_column(
        SAEnum(
            VerificationStatus,
            name="verification_status",
            create_constraint=False,
            native_enum=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=True,
        sort_order=105,
    )
    signup_source: Mapped[SignupSource] = mapped_column(
        SAEnum(SignupSource, name="signup_source"),
        default=SignupSource.organic,
        server_default=SignupSource.organic.value,
        nullable=False,
        sort_order=106,
    )

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None
