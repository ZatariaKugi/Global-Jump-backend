"""Schemas for AI-matched customer leads (PRD §3.4.3, advisor-facing)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.visa_types import OptionalVisaType
from app.models.advisor_lead import AdvisorLeadStatus


class AdvisorLeadRead(BaseModel):
    id: uuid.UUID
    seeker_id: uuid.UUID
    seeker_name: str | None
    seeker_email: str
    assessment_id: uuid.UUID
    destination_country: str
    visa_type: OptionalVisaType
    visa_type_name: str | None = None
    match_score: float
    match_reasons: str
    status: AdvisorLeadStatus
    created_at: datetime
