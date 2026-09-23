"""Inverse matching — ranked seekers for an advisor (onboarding / leads preview).

Mirrors seeker onboarding's matched_advisors flow:
1. Hard gate — seeker intended destination ∈ advisor country expertise AND
   seeker intended visa ∈ advisor visa specializations.
2. Rule score — country + visa + language + services (same weight buckets).
3. Optional OpenAI re-rank of the shortlist.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.file_storage import resolve_media_url
from app.core.visa_types import parse_visa_type
from app.models.advisor_profile import AdvisorProfile
from app.models.seeker_profile import (
    SeekerIntendedDestination,
    SeekerIntendedVisaType,
    SeekerProfile,
)
from app.models.user import User, UserRole
from app.schemas.advisor_profile import SeekerMatchRead
from app.services import ai_advisor_match_service, matching_weights_service
from app.services.ai_advisor_match_service import (
    AI_CANDIDATE_POOL,
    AiMatchFailure,
    AiRerankItem,
    blend_scores,
)
from app.services.matching_weights_service import DEFAULT_CONFIG, MatchingWeightConfig
from app.services.seeker_profile_service import (
    intended_destination_codes,
    intended_visa_type_values,
    needed_service_ids,
    preferred_language_names,
)

DEFAULT_LIMIT = 5


def _advisor_countries(profile: AdvisorProfile) -> set[str]:
    return {c.country_code.upper() for c in (profile.country_expertise or []) if c.country_code}


def _advisor_visa_values(profile: AdvisorProfile) -> set[str]:
    out: set[str] = set()
    for row in profile.visa_specializations or []:
        parsed = parse_visa_type(row.specialization)
        if parsed is not None:
            out.add(parsed.value)
    return out


def _advisor_languages(profile: AdvisorProfile) -> set[str]:
    return {
        lang.language.strip().casefold()
        for lang in (profile.languages or [])
        if lang.language and lang.language.strip()
    }


def _advisor_services(profile: AdvisorProfile) -> set[uuid.UUID]:
    return {
        row.service_id or row.id
        for row in (profile.offered_services or [])
    }


def score_seeker_for_advisor(
    profile: AdvisorProfile,
    seeker: SeekerProfile,
    *,
    weights: MatchingWeightConfig = DEFAULT_CONFIG,
) -> float:
    """Weighted fit of a seeker to this advisor. ``0`` when hard gates fail."""
    destinations = set(intended_destination_codes(seeker))
    visas = {
        parsed.value
        for raw in intended_visa_type_values(seeker)
        if (parsed := parse_visa_type(raw)) is not None
    }
    if not destinations or not visas:
        return 0.0
    advisor_countries = _advisor_countries(profile)
    advisor_visas = _advisor_visa_values(profile)
    if not (destinations & advisor_countries):
        return 0.0
    if not (visas & advisor_visas):
        return 0.0

    score = float(weights.country) + float(weights.visa)

    seeker_langs = {
        name.strip().casefold()
        for name in preferred_language_names(seeker)
        if name and name.strip()
    }
    advisor_langs = _advisor_languages(profile)
    if seeker_langs and advisor_langs and (seeker_langs & advisor_langs):
        score += float(weights.language)

    needed = set(needed_service_ids(seeker))
    offered = _advisor_services(profile)
    if needed and offered and (needed & offered):
        score += float(weights.services)
    elif not needed:
        score += float(weights.services) * 0.5

    return round(min(score, 100.0), 2)


def _rule_reasons(profile: AdvisorProfile, seeker: SeekerProfile) -> str:
    parts: list[str] = []
    destinations = set(intended_destination_codes(seeker)) & _advisor_countries(profile)
    if destinations:
        parts.append(f"Destination {', '.join(sorted(destinations))}")
    visas = {
        parsed.value
        for raw in intended_visa_type_values(seeker)
        if (parsed := parse_visa_type(raw)) is not None
    } & _advisor_visa_values(profile)
    if visas:
        parts.append(f"Visa {', '.join(sorted(visas))}")
    seeker_langs = {
        name.strip().casefold()
        for name in preferred_language_names(seeker)
        if name and name.strip()
    }
    if seeker_langs & _advisor_languages(profile):
        parts.append("Language match")
    needed = set(needed_service_ids(seeker))
    if needed & _advisor_services(profile):
        parts.append("Services match")
    return "; ".join(parts) if parts else "Profile fit"


def _apply_ai_blend(
    matches: list[SeekerMatchRead],
    items: list[AiRerankItem],
) -> list[SeekerMatchRead]:
    by_id = {item.advisor_id: item for item in items}
    blended: list[SeekerMatchRead] = []
    for match in matches:
        item = by_id.get(match.user_id)
        rule = match.rule_score if match.rule_score is not None else match.match_score
        if item is None or item.ai_score is None:
            blended.append(
                match.model_copy(
                    update={
                        "rule_score": rule,
                        "ai_score": None,
                        "match_score": rule,
                    }
                )
            )
            continue
        blended.append(
            match.model_copy(
                update={
                    "rule_score": rule,
                    "ai_score": item.ai_score,
                    "match_score": blend_scores(rule, item.ai_score),
                    "match_reasons": item.reason or match.match_reasons,
                }
            )
        )
    blended.sort(key=lambda m: m.match_score, reverse=True)
    return blended


async def match_seekers_for_advisor(
    session: AsyncSession,
    advisor_user_id: uuid.UUID,
    profile: AdvisorProfile,
    *,
    limit: int = DEFAULT_LIMIT,
    settings: Settings | None = None,
    use_ai: bool = True,
) -> tuple[list[SeekerMatchRead], AiMatchFailure | None]:
    """Rank seekers whose onboarding intent fits this advisor profile."""
    countries = _advisor_countries(profile)
    visas = _advisor_visa_values(profile)
    if not countries or not visas:
        return [], None

    weights = await matching_weights_service.get_config(session)
    cfg = settings or get_settings()

    stmt = (
        select(User, SeekerProfile)
        .join(SeekerProfile, SeekerProfile.user_id == User.id)
        .where(User.role == UserRole.seeker)
        .where(User.is_active.is_(True))
        .where(
            or_(
                SeekerProfile.intended_destination.in_(sorted(countries)),
                SeekerProfile.id.in_(
                    select(SeekerIntendedDestination.profile_id).where(
                        SeekerIntendedDestination.country_code.in_(sorted(countries))
                    )
                ),
            )
        )
        .where(
            or_(
                SeekerProfile.intended_visa_type.in_(sorted(visas)),
                SeekerProfile.id.in_(
                    select(SeekerIntendedVisaType.profile_id).where(
                        SeekerIntendedVisaType.visa_type.in_(sorted(visas))
                    )
                ),
            )
        )
    )
    rows = list((await session.execute(stmt)).all())

    matches: list[SeekerMatchRead] = []
    for user, seeker in rows:
        score = score_seeker_for_advisor(profile, seeker, weights=weights)
        if score <= 0:
            continue
        destinations = intended_destination_codes(seeker)
        visa_types = intended_visa_type_values(seeker)
        matches.append(
            SeekerMatchRead(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                profile_photo_url=resolve_media_url(seeker.profile_photo_url, cfg),
                intended_destination=(
                    destinations[0] if destinations else seeker.intended_destination
                ),
                intended_visa_type=visa_types[0] if visa_types else seeker.intended_visa_type,
                preferred_languages=preferred_language_names(seeker),
                service_ids=needed_service_ids(seeker),
                match_score=score,
                match_reasons=_rule_reasons(profile, seeker),
                rule_score=score,
                ai_score=None,
            )
        )

    matches.sort(key=lambda m: m.match_score, reverse=True)

    ai_failure: AiMatchFailure | None = None
    if use_ai and matches:
        outcome = await ai_advisor_match_service.rerank_seekers(
            advisor_user_id=advisor_user_id,
            profile=profile,
            candidates=matches[:AI_CANDIDATE_POOL],
            settings=cfg,
        )
        if outcome.failure is not None:
            ai_failure = outcome.failure
        else:
            matches = _apply_ai_blend(matches, outcome.items)

    return matches[:limit], ai_failure
