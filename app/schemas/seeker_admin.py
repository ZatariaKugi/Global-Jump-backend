"""Admin "Visa Seeker Management" schemas — seeker-specific enriched view."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.core.visa_types import OptionalVisaType
from app.models.seeker_profile import EducationLevel, EmploymentStatus
from app.schemas.user_admin import AccountStatus


class SeekerListRead(BaseModel):
    id: uuid.UUID
    full_name: str | None
    email: str
    country_of_residence: str | None
    country_of_residence_name: str | None
    intended_visa_type: OptionalVisaType
    intended_visa_type_name: str | None
    status: AccountStatus
    ai_assessment_count: int
    total_bookings: int
    created_at: datetime


class SeekerDetailRead(SeekerListRead):
    nationality: str | None
    nationality_name: str | None
    intended_destination: str | None
    intended_destination_name: str | None
    education_level: EducationLevel | None
    employment_status: EmploymentStatus | None


class SeekerCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    country_of_residence: str | None = Field(default=None, min_length=2, max_length=2)
    intended_visa_type: OptionalVisaType = None
