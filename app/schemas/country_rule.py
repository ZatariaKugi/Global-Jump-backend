"""Request/response schemas for Country Rules & Policies (admin)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.countries import country_name
from app.core.official_sources import official_source
from app.core.visa_types import RequiredVisaType, visa_type_name
from app.models.country_rule import CountryRule, RulePublishStatus
from app.schemas.assessment import SupportedCountryCode


class CountryRuleGenerateRequest(BaseModel):
    country_code: SupportedCountryCode
    visa_type: RequiredVisaType


class CountryRuleUpdate(BaseModel):
    """Edit a draft. Omitted fields are left unchanged; provided lists replace."""

    summary: str | None = Field(default=None, max_length=4000)
    requirements: list[str] | None = Field(default=None, max_length=50)
    pitfalls: list[str] | None = Field(default=None, max_length=50)
    process_notes: list[str] | None = Field(default=None, max_length=50)
    source_url: str | None = Field(default=None, max_length=1000)


class CountryRuleRead(BaseModel):
    id: uuid.UUID
    country_code: str
    country_name: str | None
    visa_type: str
    visa_type_label: str | None
    version: int
    status: RulePublishStatus
    summary: str | None
    requirements: list[str]
    pitfalls: list[str]
    process_notes: list[str]
    source_url: str | None
    retrieved_url: str | None
    grounded: bool
    generated_by_model: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    official_source_label: str | None
    official_source_url: str | None

    @classmethod
    def from_model(cls, rule: CountryRule) -> CountryRuleRead:
        source = official_source(rule.country_code)
        return cls(
            id=rule.id,
            country_code=rule.country_code,
            country_name=country_name(rule.country_code),
            visa_type=rule.visa_type,
            visa_type_label=visa_type_name(rule.visa_type),
            version=rule.version,
            status=rule.status,
            summary=rule.summary,
            requirements=[r.text for r in rule.requirements],
            pitfalls=[p.text for p in rule.pitfalls],
            process_notes=[n.text for n in rule.process_notes],
            source_url=rule.source_url,
            retrieved_url=rule.retrieved_url,
            grounded=rule.grounded,
            generated_by_model=rule.generated_by_model,
            published_at=rule.published_at,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
            official_source_label=source.label if source else None,
            official_source_url=source.start_url if source else None,
        )
