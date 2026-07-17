"""Combined AI Engine settings payload for the admin dashboard."""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.assessment_threshold import AssessmentThresholdRead
from app.schemas.matching_weights import MatchingWeightsRead


class EngineSettingsRead(BaseModel):
    thresholds: AssessmentThresholdRead | None
    matching_weights: MatchingWeightsRead
