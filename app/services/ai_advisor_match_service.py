"""OpenAI re-ranking for advisor recommendations (hybrid with rule scores).

The deterministic matcher remains the gatekeeper (country required, visa-only
excluded). This service only re-ranks an already-filtered shortlist and never
introduces advisors that were not provided. Failures return rule-based ranking with
structured failure metadata so callers can still serve weight-scored advisors.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from openai.types.shared_params.response_format_json_schema import (
    JSONSchema,
    ResponseFormatJSONSchema,
)
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.countries import country_name
from app.core.openai_client import get_openai_client
from app.core.visa_types import visa_type_name
from app.models.assessment import Assessment
from app.schemas.advisor_profile import SeekerMatchRead
from app.schemas.assessment import AdvisorMatchRead, AiMatchStatusRead

log = structlog.get_logger()

# How many rule-ranked candidates we send to the model.
AI_CANDIDATE_POOL = 25

# Final display score = rule * RULE_BLEND + ai * AI_BLEND
RULE_BLEND = 0.65
AI_BLEND = 0.35

_SYSTEM_PROMPT = """\
You are an advisor-matching assistant for GlobleJump. You receive a seeker's
visa case (destination country + visa type, optional soft profile signals) and a
shortlist of advisors that already passed a hard country-expertise filter.

Your job:
- Re-rank ONLY the provided advisors from best to worst fit for this seeker.
- Prefer advisors who match BOTH destination country and visa type.
- Then prefer language fit, rating, experience, price fit vs seeker income band,
  and availability signals present in the advisor cards.
- Never invent advisors. Never include an advisor_id that was not in the input.
- Never change or comment on visa eligibility scores.
- Give each advisor an ai_score from 0–100 and a short reason (under 180 chars).
- Respond only with JSON matching the required schema.
"""

_RESPONSE_FORMAT = ResponseFormatJSONSchema(
    type="json_schema",
    json_schema=JSONSchema(
        name="advisor_rerank",
        strict=True,
        schema={
            "type": "object",
            "properties": {
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "advisor_id": {"type": "string"},
                            "ai_score": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["advisor_id", "ai_score", "reason"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["matches"],
            "additionalProperties": False,
        },
    ),
)


class _RawMatch(BaseModel):
    advisor_id: str
    ai_score: float = Field(ge=0, le=100)
    reason: str


class _RawRerank(BaseModel):
    matches: list[_RawMatch]


@dataclass(frozen=True)
class AiRerankItem:
    advisor_id: uuid.UUID
    ai_score: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class SeekerMatchCase:
    """Destination/visa case for rule scoring + OpenAI re-rank.

    Built from a completed assessment when available, otherwise from the seeker
    profile (onboarding intent). Soft fields are optional ranking hints only.

    ``destination_country`` / ``visa_type`` remain the primary (first) values for
    prompts and legacy callers. When ``destination_countries`` / ``visa_types``
    are set, hard gates use any-overlap against those lists.
    """

    destination_country: str
    visa_type: str
    seeker_id: uuid.UUID | None = None
    assessment_id: uuid.UUID | None = None
    preferred_languages: tuple[str, ...] = ()
    needed_services: tuple[str, ...] = ()
    timezone: str | None = None
    nationality: str | None = None
    country_of_residence: str | None = None
    annual_income_band: str | None = None
    eligibility_tier: str | None = None
    eligibility_score: float | None = None
    context_source: str = "profile"  # assessment | profile
    destination_countries: tuple[str, ...] = ()
    visa_types: tuple[str, ...] = ()

    def destinations_for_gate(self) -> tuple[str, ...]:
        if self.destination_countries:
            return tuple(d.upper() for d in self.destination_countries if d)
        if self.destination_country:
            return (self.destination_country.upper(),)
        return ()

    def visas_for_gate(self) -> tuple[str, ...]:
        if self.visa_types:
            return tuple(v for v in self.visa_types if v)
        if self.visa_type:
            return (self.visa_type,)
        return ()


# Back-compat alias used by older call sites / imports.
SeekerMatchContext = SeekerMatchCase


def case_from_assessment(
    assessment: Assessment,
    *,
    preferred_languages: tuple[str, ...] | list[str] = (),
    needed_services: tuple[str, ...] | list[str] = (),
    timezone: str | None = None,
    nationality: str | None = None,
    country_of_residence: str | None = None,
    annual_income_band: str | None = None,
) -> SeekerMatchCase:
    """Build a match case from a completed (or in-progress) assessment row."""
    return SeekerMatchCase(
        destination_country=assessment.destination_country.upper(),
        visa_type=assessment.visa_type,
        destination_countries=(assessment.destination_country.upper(),),
        visa_types=(assessment.visa_type,),
        seeker_id=assessment.user_id,
        assessment_id=assessment.id,
        preferred_languages=tuple(preferred_languages),
        needed_services=tuple(needed_services),
        timezone=timezone,
        nationality=nationality,
        country_of_residence=country_of_residence,
        annual_income_band=annual_income_band,
        eligibility_tier=assessment.tier.value if assessment.tier else None,
        eligibility_score=float(assessment.score) if assessment.score is not None else None,
        context_source="assessment",
    )


def blend_scores(rule_score: float, ai_score: float) -> float:
    """Combine deterministic and AI scores into the seeker-facing match %."""
    return round(min(100.0, rule_score * RULE_BLEND + ai_score * AI_BLEND), 2)


def _build_user_prompt(case: SeekerMatchCase, candidates: list[AdvisorMatchRead]) -> str:
    dest = case.destination_country.upper()
    visa = case.visa_type
    languages = ", ".join(case.preferred_languages) if case.preferred_languages else "unknown"
    services = ", ".join(case.needed_services) if case.needed_services else "unknown"
    lines = [
        "Seeker case:",
        f"- Destination: {country_name(dest) or dest} ({dest})",
        f"- Visa type: {visa_type_name(visa) or visa}",
        f"- Preferred languages: {languages}",
        f"- Needed services: {services}",
        f"- Timezone: {case.timezone or 'unknown'}",
        f"- Nationality: {case.nationality or 'unknown'}",
        f"- Country of residence: {case.country_of_residence or 'unknown'}",
        f"- Annual income band: {case.annual_income_band or 'unknown'}",
        f"- Eligibility tier: {case.eligibility_tier or 'unknown'}",
        f"- Eligibility score: "
        f"{case.eligibility_score if case.eligibility_score is not None else 'unknown'}",
        f"- Context source: {case.context_source}",
        "",
        "Candidate advisors (already country-filtered). Re-rank all of them:",
    ]
    for c in candidates:
        visa_specs = ", ".join(c.visa_specializations) if c.visa_specializations else "none"
        countries = ", ".join(c.country_expertise) if c.country_expertise else "none"
        lines.append(
            "- "
            f"id={c.user_id}; name={c.full_name or 'Advisor'}; title={c.title or '-'}; "
            f"experience_years={c.years_of_experience}; rating={c.average_rating}; "
            f"starting_price_usd={c.starting_price_usd}; rule_score={c.match_score}; "
            f"visa_specializations=[{visa_specs}]; country_expertise=[{countries}]"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class AiMatchFailure:
    """OpenAI re-rank could not run — callers keep rule/weight-based ranking."""

    reason: str  # not_configured | openai_error
    message: str
    error: str | None = None


@dataclass(frozen=True)
class RerankOutcome:
    items: list[AiRerankItem]
    failure: AiMatchFailure | None = None


def _build_failure(
    *,
    reason: str,
    error: str | None = None,
) -> AiMatchFailure:
    if reason == "not_configured":
        return AiMatchFailure(
            reason="not_configured",
            message="AI advisor matching is not configured",
        )
    return AiMatchFailure(
        reason="openai_error",
        message="AI advisor matching is not working",
        error=error,
    )


async def rerank_advisors(
    case: SeekerMatchCase,
    candidates: list[AdvisorMatchRead],
    settings: Settings,
    *,
    seeker: SeekerMatchCase | None = None,
) -> RerankOutcome:
    """Ask OpenAI to re-rank ``candidates``. On failure, returns empty items + failure."""
    if not candidates:
        return RerankOutcome(items=[])
    if seeker is not None:
        case = SeekerMatchCase(
            destination_country=case.destination_country,
            visa_type=case.visa_type,
            seeker_id=case.seeker_id or seeker.seeker_id,
            assessment_id=case.assessment_id,
            preferred_languages=seeker.preferred_languages or case.preferred_languages,
            needed_services=seeker.needed_services or case.needed_services,
            timezone=seeker.timezone or case.timezone,
            nationality=seeker.nationality or case.nationality,
            country_of_residence=seeker.country_of_residence or case.country_of_residence,
            annual_income_band=seeker.annual_income_band or case.annual_income_band,
            eligibility_tier=case.eligibility_tier,
            eligibility_score=case.eligibility_score,
            context_source=case.context_source,
        )

    case_id = case.assessment_id or case.seeker_id
    if not settings.OPENAI_API_KEY:
        log.debug("ai_advisor_rerank_failed", reason="not_configured", case_id=str(case_id))
        return RerankOutcome(items=[], failure=_build_failure(reason="not_configured"))

    pool = candidates[:AI_CANDIDATE_POOL]
    allowed_ids = {str(c.user_id) for c in pool}

    try:
        client = get_openai_client(settings)
        if client is None:
            return RerankOutcome(items=[], failure=_build_failure(reason="not_configured"))
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(case, pool)},
            ],
            response_format=_RESPONSE_FORMAT,
            max_completion_tokens=2000,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("empty completion content")
        raw = _RawRerank.model_validate(json.loads(content))
    except Exception as exc:
        log.warning(
            "ai_advisor_rerank_failed",
            case_id=str(case_id) if case_id else None,
            model=settings.OPENAI_MODEL,
            error=str(exc),
        )
        return RerankOutcome(
            items=[],
            failure=_build_failure(reason="openai_error", error=str(exc)),
        )

    seen: set[str] = set()
    items: list[AiRerankItem] = []
    for row in raw.matches:
        if row.advisor_id not in allowed_ids or row.advisor_id in seen:
            continue
        seen.add(row.advisor_id)
        items.append(
            AiRerankItem(
                advisor_id=uuid.UUID(row.advisor_id),
                ai_score=round(float(row.ai_score), 2),
                reason=(row.reason or "").strip()[:180] or "Strong profile fit",
            )
        )

    # Append any candidates the model omitted, preserving rule order.
    # ai_score=None signals "not AI-evaluated" so blend logic keeps rule-only score.
    for c in pool:
        key = str(c.user_id)
        if key in seen:
            continue
        items.append(
            AiRerankItem(
                advisor_id=c.user_id,
                ai_score=None,
                reason="Ranked by rule score",
            )
        )

    log.info(
        "ai_advisor_rerank_generated",
        case_id=str(case_id) if case_id else None,
        context_source=case.context_source,
        model=settings.OPENAI_MODEL,
        candidates=len(pool),
        returned=len(items),
    )
    return RerankOutcome(items=items)


def ai_match_status(
    failure: AiMatchFailure | None,
    *,
    attempted: bool,
) -> AiMatchStatusRead | None:
    """Map rerank outcome to the API ``ai_match`` field."""
    if not attempted:
        return None
    if failure is None:
        return AiMatchStatusRead(available=True, ranking="ai")
    return AiMatchStatusRead(
        available=False,
        ranking="rule",
        message=failure.message,
        reason=failure.reason,
        error=failure.error,
    )


_SEEKER_SYSTEM_PROMPT = """\
You are a client-matching assistant for GlobleJump advisors. You receive an
advisor's expertise (countries, visa types, languages, services) and a shortlist
of seekers that already passed hard destination+visa filters.

Your job:
- Re-rank ONLY the provided seekers from best to worst fit for this advisor.
- Prefer language overlap and needed-services overlap with the advisor.
- Never invent seekers. Never include a seeker_id that was not in the input.
- Give each seeker an ai_score from 0–100 and a short reason (under 180 chars).
- Respond only with JSON matching the required schema.
"""

_SEEKER_RESPONSE_FORMAT = ResponseFormatJSONSchema(
    type="json_schema",
    json_schema=JSONSchema(
        name="seeker_rerank",
        strict=True,
        schema={
            "type": "object",
            "properties": {
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "seeker_id": {"type": "string"},
                            "ai_score": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["seeker_id", "ai_score", "reason"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["matches"],
            "additionalProperties": False,
        },
    ),
)


class _RawSeekerMatch(BaseModel):
    seeker_id: str
    ai_score: float = Field(ge=0, le=100)
    reason: str


class _RawSeekerRerank(BaseModel):
    matches: list[_RawSeekerMatch]


def _build_seeker_rerank_prompt(
    *,
    countries: list[str],
    visas: list[str],
    languages: list[str],
    services: list[str],
    candidates: Sequence[SeekerMatchRead],
) -> str:
    lines = [
        "Advisor profile:",
        f"- Countries served: {', '.join(countries) or 'unknown'}",
        f"- Visa specializations: {', '.join(visas) or 'unknown'}",
        f"- Languages: {', '.join(languages) or 'unknown'}",
        f"- Offered services: {', '.join(services) or 'unknown'}",
        "",
        "Candidate seekers (already destination+visa filtered). Re-rank all of them:",
    ]
    for c in candidates:
        langs = ", ".join(c.preferred_languages) if c.preferred_languages else "none"
        svcs = ", ".join(c.needed_services) if c.needed_services else "none"
        lines.append(
            "- "
            f"id={c.user_id}; name={c.full_name or 'Seeker'}; "
            f"destination={c.intended_destination}; visa={c.intended_visa_type}; "
            f"languages=[{langs}]; needed_services=[{svcs}]; "
            f"rule_score={c.match_score}"
        )
    return "\n".join(lines)


async def rerank_seekers(
    *,
    advisor_user_id: uuid.UUID,
    profile: object,
    candidates: Sequence[SeekerMatchRead],
    settings: Settings,
) -> RerankOutcome:
    """Ask OpenAI to re-rank seeker candidates for an advisor.

    ``AiRerankItem.advisor_id`` holds the seeker user id (shared blend helper).
    """
    from app.models.advisor_profile import AdvisorProfile

    if not candidates:
        return RerankOutcome(items=[])
    if not settings.OPENAI_API_KEY:
        log.debug(
            "ai_seeker_rerank_failed",
            reason="not_configured",
            advisor_id=str(advisor_user_id),
        )
        return RerankOutcome(items=[], failure=_build_failure(reason="not_configured"))

    assert isinstance(profile, AdvisorProfile)
    pool = list(candidates)[:AI_CANDIDATE_POOL]
    allowed_ids = {str(c.user_id) for c in pool}

    countries = sorted(
        {
            c.country_code.upper()
            for c in (profile.country_expertise or [])
            if c.country_code
        }
    )
    visas = sorted(
        {
            s.specialization
            for s in (profile.visa_specializations or [])
            if s.specialization
        }
    )
    languages = sorted(
        {lang.language for lang in (profile.languages or []) if lang.language}
    )
    services = sorted(
        {
            row.name
            for row in (profile.offered_services or [])
            if row.name
        }
    )

    try:
        client = get_openai_client(settings)
        if client is None:
            return RerankOutcome(items=[], failure=_build_failure(reason="not_configured"))
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": _SEEKER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_seeker_rerank_prompt(
                        countries=countries,
                        visas=visas,
                        languages=languages,
                        services=services,
                        candidates=pool,
                    ),
                },
            ],
            response_format=_SEEKER_RESPONSE_FORMAT,
            max_completion_tokens=2000,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("empty completion content")
        raw = _RawSeekerRerank.model_validate(json.loads(content))
    except Exception as exc:
        log.warning(
            "ai_seeker_rerank_failed",
            advisor_id=str(advisor_user_id),
            model=settings.OPENAI_MODEL,
            error=str(exc),
        )
        return RerankOutcome(
            items=[],
            failure=_build_failure(reason="openai_error", error=str(exc)),
        )

    seen: set[str] = set()
    items: list[AiRerankItem] = []
    for row in raw.matches:
        if row.seeker_id not in allowed_ids or row.seeker_id in seen:
            continue
        seen.add(row.seeker_id)
        items.append(
            AiRerankItem(
                advisor_id=uuid.UUID(row.seeker_id),
                ai_score=round(float(row.ai_score), 2),
                reason=(row.reason or "").strip()[:180] or "Strong client fit",
            )
        )

    for c in pool:
        key = str(c.user_id)
        if key in seen:
            continue
        items.append(
            AiRerankItem(
                advisor_id=c.user_id,
                ai_score=None,
                reason="Ranked by rule score",
            )
        )

    log.info(
        "ai_seeker_rerank_generated",
        advisor_id=str(advisor_user_id),
        model=settings.OPENAI_MODEL,
        candidates=len(pool),
        returned=len(items),
    )
    return RerankOutcome(items=items)

