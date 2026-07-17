"""Admin "Advisor Management" + "Verification Queue" schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.models.user import VerificationStatus
from app.schemas.advisor_profile import LanguageEntry
from app.schemas.booking import BookingRead


class AdvisorStatus(StrEnum):
    """Badge status for advisor admin lists.

    Prefer soft-suspend over verification: an approved-then-suspended advisor
    shows ``suspended``. Otherwise mirrors ``verification_status``.
    """

    pending = "pending"
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    suspended = "suspended"


class AdvisorManagementListRead(BaseModel):
    id: uuid.UUID
    full_name: str | None
    email: str
    profile_photo_url: str | None
    country_code: str | None  # ISO-3166 alpha-2 (e.g. "US")
    country: str | None  # display name (e.g. "United States")
    expertise: list[str]
    # Display badge: suspended wins; else verification_status (approved/pending/…).
    status: AdvisorStatus
    # Raw approval workflow — unchanged by suspend/reactivate.
    verification_status: VerificationStatus | None
    is_suspended: bool
    is_active: bool
    session_count: int
    avg_rating: float | None
    review_count: int
    earnings: float
    created_at: datetime


class AdvisorManagementDetailRead(AdvisorManagementListRead):
    title: str | None
    bio: str | None
    years_of_experience: int | None
    successful_applications: int | None
    successful_application_rate: float | None  # 0–100 percentage
    country_expertise: list[str]  # ISO codes
    country_expertise_names: list[str]  # display names parallel to country_expertise
    languages: list[LanguageEntry]
    completed_sessions: int
    credentials_pending_count: int
    credentials_verified_count: int


class AdvisorSessionRead(BookingRead):
    """Session History tab row — BookingRead plus seeker country + consultation count."""

    country_code: str | None  # seeker country_of_residence ISO-3166 alpha-2
    country_name: str | None  # display name (e.g. "United Kingdom")
    consultation_count: int  # total bookings between this seeker and this advisor


class AdvisorEarningRowRead(BaseModel):
    """One row for the advisor Earnings tab table."""

    appointment_id: str
    booking_id: uuid.UUID
    seeker_name: str | None
    seeker_email: str | None
    seeker_photo_url: str | None
    created_at: datetime
    amount_paid: float
    platform_fee: float
    advisor_earnings: float
    # Mirrors payment_service.display_status (paid/pending/refunded/failed).
    status: Literal["pending", "paid", "refunded", "failed"]


class AdvisorEarningsSummaryRead(BaseModel):
    total_earned_usd: float
    total_commission_paid_usd: float
    available_balance_usd: float
    total_payouts_usd: float
    pending_payout_usd: float
    transaction_count: int
    items: list[AdvisorEarningRowRead]


class VerificationQueueRead(BaseModel):
    """One row per advisor with >=1 pending AdvisorCredential.

    ``status`` is always ``pending`` for members of this list (fully resolved
    advisors drop off). ``ai_score`` / ``verification_result`` are a package-
    completeness heuristic over the three required onboarding document types
    (government_id, license, certification) — not a separate ML model.
    """

    advisor_id: uuid.UUID
    full_name: str | None
    email: str
    profile_photo_url: str | None
    pending_document_count: int
    earliest_submitted_at: datetime
    latest_submitted_at: datetime
    ai_score: float  # 0–100, % of required document types uploaded
    verification_result: Literal["all_passed", "needs_review"]
    status: Literal["pending", "verified", "rejected"]


class BulkCredentialReview(BaseModel):
    action: Literal["approve", "reject"]
    admin_note: str | None = Field(default=None, max_length=1000)
