"""AI policy drafting for Country Rules & Policies (§7, §13, §15).

Uses OpenAI's web-enabled Responses API to research a country's official
government immigration source and return a structured policy draft. The result
is ALWAYS a draft requiring human review — this service never publishes.

Fallback chain (per product decision):
  1. Official government domain (the registered source).
  2. Broader web search, if the official page can't be reached / is off-domain.
  3. The model's own knowledge, as a last resort.
A draft is produced in all three cases, but ``grounded`` is True only when the
retrieved URL belongs to the country's expected official domain (case 1).

Degrade-gracefully rule (mirrors ai_insight_service / email_service): any
failure — no API key, timeout, malformed response — logs and returns None. It
never raises into the request path; the caller cleans up the placeholder row.
"""

from __future__ import annotations

import json

import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.countries import country_name
from app.core.official_sources import OfficialSource, is_expected_domain, official_source
from app.core.visa_types import visa_type_name

log = structlog.get_logger()

_MAX_LIST_ITEMS = 20
_MAX_ITEM_CHARS = 1000  
_MAX_SUMMARY_CHARS = 4000  
_MAX_URL_CHARS = 1000

_SYSTEM_PROMPT = """\
You research official government immigration websites and produce a STRUCTURED
POLICY DRAFT for a single country and visa type. Your output is a first draft
that a human administrator will verify before it is ever used — never present it
as authoritative.

Rules:
1. Prefer information found on the official government page you are given. Use the
   web_search tool to open it and read it.
2. If that page does not exist, cannot be opened, or does not cover the requested
   visa type, search more broadly for the official government source for this
   country and visa type.
3. If you still cannot find official information, you may draft from your own
   knowledge — but keep it conservative and general.
4. Do NOT invent specific fees, financial thresholds, quotas, or processing times.
   Only include such figures if you actually found them on a government source.
5. If the official page does not cover the requested visa type, say so plainly in
   the summary instead of inventing requirements.
6. Report the single URL you actually relied on most in "retrieved_url" (empty
   string if you used only your own knowledge).

Respond with ONLY a JSON object of this exact shape (no markdown, no prose):
{
  "summary": string,
  "requirements": [string, ...],
  "pitfalls": [string, ...],
  "process_notes": [string, ...],
  "retrieved_url": string
}
"""


class PolicyDraft(BaseModel):
    """Internal contract between this service and country_rule_service."""

    summary: str
    requirements: list[str] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)
    process_notes: list[str] = Field(default_factory=list)
    retrieved_url: str | None = None
    grounded: bool = False
    model: str | None = None


class _RawDraft(BaseModel):
    summary: str = ""
    requirements: list[str] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)
    process_notes: list[str] = Field(default_factory=list)
    retrieved_url: str = ""


def _build_user_prompt(country_code: str, visa_type: str, source: OfficialSource) -> str:
    cname = country_name(country_code) or country_code
    vname = visa_type_name(visa_type) or visa_type
    return (
        f"Country: {cname} ({country_code})\n"
        f"Visa type: {vname} ({visa_type})\n"
        f"Official authority: {source.label}\n"
        f"Official starting URL: {source.start_url}\n\n"
        "Draft the policy (summary, typical requirements, common pitfalls, process "
        "notes) for this exact country and visa type."
    )


def _clamp_list(items: list[str]) -> list[str]:
    cleaned = [i.strip()[:_MAX_ITEM_CHARS] for i in items if i and i.strip()]
    return cleaned[:_MAX_LIST_ITEMS]


def _extract_json(text: str) -> dict[str, object]:
    """Best-effort: parse the first JSON object in the model output."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in model output")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model output is not a JSON object")
    return parsed


async def draft_policy(
    country_code: str,
    visa_type: str,
    settings: Settings,
) -> PolicyDraft | None:
    """Research and draft a policy for one (country, visa) pair.

    Returns None when OpenAI is unconfigured or the call fails for any reason;
    never raises. The returned draft is always unverified (caller stores it as a
    ``draft`` for admin review).
    """
    if not settings.OPENAI_API_KEY:
        log.debug("country_rule_ai_skipped", reason="not_configured")
        return None

    source = official_source(country_code)
    if source is None:
        log.warning("country_rule_ai_no_source", country_code=country_code)
        return None

    model = settings.OPENAI_WEBSEARCH_MODEL
    try:
        client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=settings.OPENAI_WEBSEARCH_TIMEOUT_SECONDS,
            max_retries=1,
        )
      
        response = await client.responses.create(
            model=model,
            tools=[{"type": "web_search"}],
            input=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(country_code, visa_type, source)},
            ],
        )
        content = response.output_text
        if not content:
            raise ValueError("empty responses output_text")
        raw = _RawDraft.model_validate(_extract_json(content))
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never 500
        log.warning(
            "country_rule_ai_failed",
            country_code=country_code,
            visa_type=visa_type,
            model=model,
            error=str(exc),
        )
        return None

    retrieved = (raw.retrieved_url or "").strip()[:_MAX_URL_CHARS] or None
    grounded = is_expected_domain(retrieved, country_code)
    log.info(
        "country_rule_ai_drafted",
        country_code=country_code,
        visa_type=visa_type,
        model=model,
        grounded=grounded,
        retrieved_url=retrieved,
    )
    return PolicyDraft(
        summary=(raw.summary or "").strip()[:_MAX_SUMMARY_CHARS],
        requirements=_clamp_list(raw.requirements),
        pitfalls=_clamp_list(raw.pitfalls),
        process_notes=_clamp_list(raw.process_notes),
        retrieved_url=retrieved,
        grounded=grounded,
        model=model,
    )
