"""OpenAI re-ranking for advisor recommendations (hybrid with rule scores).

The deterministic matcher remains the gatekeeper (country required, visa-only
excluded). This service only re-ranks an already-filtered shortlist and never
introduces advisors that were not provided. Failures degrade to None so callers
keep pure rule ranking — same pattern as ``ai_insight_service``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import structlog
from openai import AsyncOpenAI
from openai.types.shared_params.response_format_json_schema import (
    JSONSchema,
    ResponseFormatJSONSchema,
)
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.countries import country_name
from app.core.visa_types import visa_type_name
from app.models.assessment import Assessment
from app.schemas.assessment import AdvisorMatchRead

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
    ai_score: float
    reason: str


@dataclass(frozen=True)
class SeekerMatchCase:
    """Destination/visa case for rule scoring + OpenAI re-rank.

    Built from a completed assessment when available, otherwise from the seeker
    profile (onboarding intent). Soft fields are optional ranking hints only.
    """

    destination_country: str
    visa_type: str
    seeker_id: uuid.UUID | None = None
    assessment_id: uuid.UUID | None = None
    preferred_language: str | None = None
    timezone: str | None = None
    nationality: str | None = None
    country_of_residence: str | None = None
    annual_income_band: str | None = None
    eligibility_tier: str | None = None
    eligibility_score: float | None = None
    context_source: str = "profile"  # assessment | profile


# Back-compat alias used by older call sites / imports.
SeekerMatchContext = SeekerMatchCase


def case_from_assessment(
    assessment: Assessment,
    *,
    preferred_language: str | None = None,
    timezone: str | None = None,
    nationality: str | None = None,
    country_of_residence: str | None = None,
    annual_income_band: str | None = None,
) -> SeekerMatchCase:
    """Build a match case from a completed (or in-progress) assessment row."""
    return SeekerMatchCase(
        destination_country=assessment.destination_country.upper(),
        visa_type=assessment.visa_type,
        seeker_id=assessment.user_id,
        assessment_id=assessment.id,
        preferred_language=preferred_language,
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
    lines = [
        "Seeker case:",
        f"- Destination: {country_name(dest) or dest} ({dest})",
        f"- Visa type: {visa_type_name(visa) or visa}",
        f"- Preferred language: {case.preferred_language or 'unknown'}",
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
        lines.append(
            "- "
            f"id={c.user_id}; name={c.full_name or 'Advisor'}; title={c.title or '-'}; "
            f"experience_years={c.years_of_experience}; rating={c.average_rating}; "
            f"starting_price_usd={c.starting_price_usd}; rule_score={c.match_score}"
        )
    return "\n".join(lines)


async def rerank_advisors(
    case: SeekerMatchCase,
    candidates: list[AdvisorMatchRead],
    settings: Settings,
    *,
    seeker: SeekerMatchCase | None = None,
) -> list[AiRerankItem] | None:
    """Ask OpenAI to re-rank ``candidates``. Returns None on skip/failure.

    ``seeker`` is accepted for back-compat; when provided it overlays soft fields
    onto ``case`` without changing destination/visa.
    """
    if not candidates:
        return []
    if seeker is not None:
        case = SeekerMatchCase(
            destination_country=case.destination_country,
            visa_type=case.visa_type,
            seeker_id=case.seeker_id or seeker.seeker_id,
            assessment_id=case.assessment_id,
            preferred_language=seeker.preferred_language or case.preferred_language,
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
        log.debug("ai_advisor_rerank_skipped", reason="not_configured")
        return None

    pool = candidates[:AI_CANDIDATE_POOL]
    allowed_ids = {str(c.user_id) for c in pool}

    try:
        client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=settings.OPENAI_TIMEOUT_SECONDS,
            max_retries=1,
        )
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
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
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        log.warning(
            "ai_advisor_rerank_failed",
            case_id=str(case_id) if case_id else None,
            model=settings.OPENAI_MODEL,
            error=str(exc),
        )
        return None

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
    for c in pool:
        key = str(c.user_id)
        if key in seen:
            continue
        items.append(
            AiRerankItem(
                advisor_id=c.user_id,
                ai_score=c.match_score,
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
    return items
