"""AI narrative insights for completed assessments (PRD §3.4).

OpenAI GPT explains the deterministic result — it never overrides it. The
deterministic engine remains the authoritative scorer; this service only
turns the score, tier, and answer transcript into seeker-facing narrative
(strengths, weaknesses, missing requirements, an improvement summary).

Mirrors email_service's degrade-gracefully rule: any failure (no API key,
timeout, rate limit, malformed response) logs and returns None — it never
surfaces into the request path, so assessments always complete.
"""

from __future__ import annotations

import json
import re

import structlog
from openai import AsyncOpenAI
from openai.types.shared_params.response_format_json_schema import (
    JSONSchema,
    ResponseFormatJSONSchema,
)
from pydantic import BaseModel

from app.core.config import Settings
from app.models.assessment import Assessment, AssessmentQuestion, AssessmentQuestionOption
from app.schemas.seeker_profile import OnboardingSubmit

log = structlog.get_logger()

_MAX_ITEMS = 5
_MAX_ITEM_CHARS = 500  # assessment_insights.text column limit
_MAX_SUMMARY_CHARS = 2000  # assessments.ai_summary column limit
# Strengths are only credible from categories the deterministic engine scored
# well. The model tags each strength with its category; anything below this
# score (or with an unrecognizable category) is dropped server-side, because
# models pad the strengths list with silver linings on weak profiles no matter
# how firmly the prompt forbids it.
_STRENGTH_MIN_CATEGORY_SCORE = 70.0

_SYSTEM_PROMPT = """\
You are a visa eligibility explainer for GlobleJump, a platform that connects visa
seekers with immigration advisors. You are given the result of a deterministic,
rules-based eligibility assessment (overall score, eligibility tier, per-category
scores) together with the applicant's questionnaire answers. Explain that result in
plain, factual, encouraging language written directly to the applicant.

Rules:
- Never contradict or second-guess the provided score, tier, or category scores.
- Base every statement only on the answers provided. Never invent facts, documents,
  numbers, or circumstances that do not appear in the answers.
- Do not give legal advice, cite laws or regulations, or guarantee any visa outcome.
- "strengths": aspects of the applicant's answers that genuinely support their
  application. At most 5 items. Each item is an object with "category" (the exact
  bracketed category name of the answer it comes from) and "text" (one short
  sentence under 200 characters). Only cite categories whose category score is
  70/100 or higher. Never reframe a negative or weak answer as a strength ("your
  offence was a long time ago", "you at least have a basic education"). If few or
  no categories score 70+, return fewer items — an empty strengths list is a
  normal, correct output for a weak profile.
- "weaknesses": aspects of the answers that weaken the application. At most 5
  plain sentences (no category tags or bracket prefixes), same length limit.
- "missing_requirements": concrete things the applicant indicated they lack that are
  typically expected for this destination and visa type. At most 5 items, same
  format. Use an empty list if nothing is missing.
- "summary": one paragraph of practical improvement suggestions, under 900
  characters, second person ("you"), no bullet points.
- Respond only with JSON matching the required schema.
"""

# No maxItems/maxLength inside the schema — strict-mode keyword support varies
# across models and an unsupported keyword is a hard 400. Limits live in the
# prompt and are enforced defensively in Python before persisting.
_RESPONSE_FORMAT = ResponseFormatJSONSchema(
    type="json_schema",
    json_schema=JSONSchema(
        name="assessment_insights",
        strict=True,
        schema={
            "type": "object",
            "properties": {
                "strengths": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["category", "text"],
                        "additionalProperties": False,
                    },
                },
                "weaknesses": {"type": "array", "items": {"type": "string"}},
                "missing_requirements": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
            "required": ["strengths", "weaknesses", "missing_requirements", "summary"],
            "additionalProperties": False,
        },
    ),
)


class _StrengthItem(BaseModel):
    category: str
    text: str


class _RawInsights(BaseModel):
    """Shape returned by the model (strengths carry their source category)."""

    strengths: list[_StrengthItem]
    weaknesses: list[str]
    missing_requirements: list[str]
    summary: str


class InsightPayload(BaseModel):
    """Internal contract between this service and assessment_service — not an API schema."""

    strengths: list[str]
    weaknesses: list[str]
    missing_requirements: list[str]
    summary: str


def _category_key(raw: str) -> str:
    # Tolerate models echoing decoration: "[nationality]", "nationality (category
    # score 100/100)", "Visa Refusals", etc.
    bare = raw.split(",")[0].split("(")[0].strip().strip("[]").strip()
    return bare.lower().replace(" ", "_")


def _filter_strengths(items: list[_StrengthItem], assessment: Assessment) -> list[str]:
    """Keep only strengths whose deterministic category score clears the bar.

    Unrecognized categories are dropped too: a strength we cannot ground in the
    deterministic result must not be shown to the applicant.
    """
    scores = {_category_key(str(cs.category)): cs.score for cs in assessment.category_scores or []}
    kept = [
        item.text
        for item in items
        if scores.get(_category_key(item.category), -1.0) >= _STRENGTH_MIN_CATEGORY_SCORE
    ]
    if len(kept) < len(items):
        log.info(
            "ai_insights_strengths_filtered",
            assessment_id=str(assessment.id),
            dropped=len(items) - len(kept),
            model_categories=[item.category for item in items],
            known_categories=sorted(scores),
        )
    return kept


def _build_user_prompt(
    assessment: Assessment,
    answered: list[tuple[AssessmentQuestion, AssessmentQuestionOption]],
) -> str:
    lines = [
        f"Destination country: {assessment.destination_country}",
        f"Visa type: {assessment.visa_type}",
        f"Overall eligibility score: {assessment.score}/100",
        f"Eligibility tier: {assessment.tier.value if assessment.tier else 'unknown'}",
        "",
        "Category scores:",
    ]
    for cs in assessment.category_scores or []:
        lines.append(f"- {cs.category}: {cs.score}/100")
    # Tag each answer with its category score so the model can apply the
    # "strengths only from categories scoring 70+" rule without cross-referencing.
    cat_scores = {cs.category: cs.score for cs in assessment.category_scores or []}
    lines.append("")
    lines.append("Questionnaire answers:")
    for question, option in answered:
        category = question.category.value if question.category is not None else "uncategorized"
        score_note = (
            f" (category score {cat_scores[category]}/100)" if category in cat_scores else ""
        )
        lines.append(f"- [{category}]{score_note} {question.text}")
        lines.append(f"  Answer: {option.text}")
    return "\n".join(lines)


def _clamp(items: list[str]) -> list[str]:
    # Models sometimes leak the prompt's "[category]" answer tags into item text.
    return [
        re.sub(r"^\s*\[[^\]]{1,40}\]\s*", "", text)[:_MAX_ITEM_CHARS] for text in items[:_MAX_ITEMS]
    ]


async def generate_insights(
    assessment: Assessment,
    answered: list[tuple[AssessmentQuestion, AssessmentQuestionOption]],
    settings: Settings,
) -> InsightPayload | None:
    """Generate narrative insights for a just-scored assessment.

    Precondition: the caller has already set ``score``, ``tier``, and
    ``category_scores`` on the assessment — the prompt is grounded on them.

    Returns None when OpenAI is unconfigured or the call fails for any
    reason; never raises.
    """
    if not settings.OPENAI_API_KEY:
        log.debug("ai_insights_skipped", reason="not_configured")
        return None

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
                {"role": "user", "content": _build_user_prompt(assessment, answered)},
            ],
            response_format=_RESPONSE_FORMAT,
            max_completion_tokens=2000,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("empty completion content")
        raw = _RawInsights.model_validate(json.loads(content))
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never 500
        log.warning(
            "ai_insights_failed",
            assessment_id=str(assessment.id),
            model=settings.OPENAI_MODEL,
            error=str(exc),
        )
        return None

    log.info("ai_insights_generated", assessment_id=str(assessment.id), model=settings.OPENAI_MODEL)
    return InsightPayload(
        strengths=_clamp(_filter_strengths(raw.strengths, assessment)),
        weaknesses=_clamp(raw.weaknesses),
        missing_requirements=_clamp(raw.missing_requirements),
        summary=raw.summary[:_MAX_SUMMARY_CHARS],
    )


# ── Onboarding Step 5 suggestions (PRD onboarding wizard) ───────────────────
#
# Lightweight, standalone from the assessment insights above: no deterministic
# score/tier exists yet at onboarding time, so this has nothing to ground
# against — just a rough, generative nudge from the wizard fields collected
# in Steps 1-4 (and Steps 5-6 if the applicant filled them in).

_ONBOARDING_SYSTEM_PROMPT = """\
You are a visa readiness assistant for GlobleJump, a platform that connects visa
seekers with immigration advisors. You are given the destination country, intended
visa type, and other details a user just entered while signing up — before they
have taken any formal eligibility assessment.

Rules:
- Base every suggestion only on the details provided. Never invent facts, documents,
  or circumstances that do not appear in the input.
- Do not give legal advice, cite laws or regulations, or guarantee any visa outcome.
- Return 3 to 5 short, practical, encouraging suggestions (each under 200
  characters) for next steps the applicant could take to strengthen their
  application for that destination and visa type — e.g. documents to gather,
  information worth having ready, or things to research next.
- Respond only with JSON matching the required schema.
"""

_ONBOARDING_RESPONSE_FORMAT = ResponseFormatJSONSchema(
    type="json_schema",
    json_schema=JSONSchema(
        name="onboarding_suggestions",
        strict=True,
        schema={
            "type": "object",
            "properties": {
                "suggestions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["suggestions"],
            "additionalProperties": False,
        },
    ),
)


class _RawOnboardingSuggestions(BaseModel):
    suggestions: list[str]


def _build_onboarding_prompt(data: OnboardingSubmit) -> str:
    lines = [
        f"Intended visa type: {data.intended_visa_type}",
        f"Intended destination country: {data.intended_destination}",
        f"Annual income band: {data.annual_income_band}",
        "Travel history: " + (data.countries_visited or "not specified"),
    ]
    if data.matching_opportunities:
        categories = ", ".join(data.matching_opportunities)
        lines.append(f"Opportunity categories the applicant wants matched: {categories}")
    if data.nationality:
        lines.append(f"Nationality: {data.nationality}")
    if data.education_level:
        lines.append(f"Education level: {data.education_level.value}")
    if data.employment_status:
        lines.append(f"Employment status: {data.employment_status.value}")
    return "\n".join(lines)


async def generate_onboarding_suggestions(data: OnboardingSubmit, settings: Settings) -> list[str]:
    """Generate Step 5 AI suggestions from onboarding wizard data.

    Degrades gracefully like generate_insights: any failure (no API key,
    timeout, malformed response) logs and returns an empty list — it never
    blocks onboarding completion.
    """
    if not settings.OPENAI_API_KEY:
        log.debug("onboarding_suggestions_skipped", reason="not_configured")
        return []

    try:
        client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=settings.OPENAI_TIMEOUT_SECONDS,
            max_retries=1,
        )
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _ONBOARDING_SYSTEM_PROMPT},
                {"role": "user", "content": _build_onboarding_prompt(data)},
            ],
            response_format=_ONBOARDING_RESPONSE_FORMAT,
            max_completion_tokens=800,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("empty completion content")
        raw = _RawOnboardingSuggestions.model_validate(json.loads(content))
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never 500
        log.warning("onboarding_suggestions_failed", model=settings.OPENAI_MODEL, error=str(exc))
        return []

    log.info("onboarding_suggestions_generated", model=settings.OPENAI_MODEL)
    return _clamp(raw.suggestions)
