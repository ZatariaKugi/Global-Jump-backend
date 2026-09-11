"""AI advisor matching — ranked shortlist for seekers (PRD §3.4.3).

Hybrid pipeline:
1. Hard gate — approved/active advisors with destination-country expertise
   AND matching visa specialization. Country-only or visa-only matches are
   excluded.
2. Rule score — destination 25 + visa 25 + language 15 + services 15 +
   experience 10 + rating 10 (admin-configurable; defaults sum to 100).
3. AI re-rank — OpenAI reorders the top rule-scored pool and blends scores.
   When ``use_ai=True`` and OpenAI fails, rule/weight-based ranking is kept and
   failure metadata is returned for the API layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.visa_types import parse_visa_type
from app.models.advisor_profile import (
    AdvisorCountryExpertise,
    AdvisorProfile,
    AdvisorVisaSpecialization,
)
from app.models.assessment import Assessment, AssessmentStatus
from app.models.seeker_profile import SeekerProfile
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.assessment import AdvisorMatchRead
from app.services import ai_advisor_match_service, matching_weights_service, review_service
from app.services.advisor_profile_service import starting_price_usd
from app.services.advisor_search_service import apply_integrations_ready_filter
from app.services.ai_advisor_match_service import (
    AI_CANDIDATE_POOL,
    AiMatchFailure,
    SeekerMatchCase,
    blend_scores,
    case_from_assessment,
)
from app.services.matching_weights_service import DEFAULT_CONFIG, MatchingWeightConfig
from app.services.seeker_profile_service import (
    intended_destination_codes,
    intended_visa_type_values,
    needed_service_ids,
    preferred_language_names,
)

DEFAULT_LIMIT = 5


def has_country_expertise(profile: AdvisorProfile, destination: str) -> bool:
    """True when the advisor lists the seeker's destination country."""
    countries = {c.country_code.upper() for c in (profile.country_expertise or [])}
    return destination.upper() in countries


def has_any_country_expertise(profile: AdvisorProfile, destinations: Sequence[str]) -> bool:
    """True when the advisor covers any of the seeker's intended destinations."""
    return any(has_country_expertise(profile, dest) for dest in destinations if dest)


def has_visa_specialization(profile: AdvisorProfile, visa_type: str) -> bool:
    """True when the advisor specializes in the seeker's visa type."""
    specializations = {
        parsed
        for s in (profile.visa_specializations or [])
        if (parsed := parse_visa_type(s.specialization)) is not None
    }
    target = parse_visa_type(visa_type)
    return target is not None and target in specializations


def has_any_visa_specialization(profile: AdvisorProfile, visa_types: Sequence[str]) -> bool:
    """True when the advisor specializes in any of the seeker's intended visas."""
    return any(has_visa_specialization(profile, visa) for visa in visa_types if visa)


def _language_points(
    profile: AdvisorProfile,
    preferred_languages: list[str] | None,
    weight: float,
) -> float:
    """Full language weight only when a seeker language is in the advisor's list.

    No half credit: missing seeker preference, missing advisor languages, or
    no overlap all score ``0``.
    """
    prefs = [p.strip().casefold() for p in (preferred_languages or []) if p and p.strip()]
    if not prefs:
        return 0.0
    if not profile.languages:
        return 0.0

    advisor_langs = {
        lang.language.strip().casefold()
        for lang in profile.languages
        if lang.language and lang.language.strip()
    }
    if any(pref in advisor_langs for pref in prefs):
        return weight
    return 0.0


def _services_points(
    profile: AdvisorProfile,
    needed_services: list[uuid.UUID] | None,
    weight: float,
) -> float:
    """Full services weight when any needed ID is offered by the advisor."""
    needed = set(needed_services or [])
    if not needed:
        return weight * 0.5
    offered = {row.service_id or row.id for row in (profile.offered_services or [])}
    if not offered:
        return 0.0
    if any(service in offered for service in needed):
        return weight
    return 0.0


def _experience_points(years: int | None, weight: float) -> float:
    """Scale experience into the configured weight bucket."""
    if years is None or years <= 0:
        return 0.0
    if years >= 15:
        return weight
    if years >= 5:
        return weight * (0.5 + 0.5 * min((years - 5) / 10.0, 1.0))
    return weight * 0.5 * (years / 5.0)


def _rating_points(average_rating: float | None, weight: float) -> float:
    """Scale verified platform rating (0–5) into the rating weight."""
    if average_rating is None or average_rating <= 0:
        return 0.0
    return weight * min(average_rating / 5.0, 1.0)


def score_advisor_for_assessment(
    profile: AdvisorProfile,
    destination: str | Sequence[str],
    visa_type: str | Sequence[str],
    average_rating: float | None,
    *,
    weights: MatchingWeightConfig = DEFAULT_CONFIG,
    preferred_languages: list[str] | None = None,
    needed_services: list[uuid.UUID] | None = None,
) -> float:
    """Weighted match score for recommendations.

    Returns ``0`` when the advisor lacks destination-country expertise or the
    seeker's visa specialization (both are hard gates). Accepts a single
    destination/visa or sequences (any-overlap hard gates).
    """
    destinations = (
        [destination]
        if isinstance(destination, str)
        else [d for d in destination if d]
    )
    visas = [visa_type] if isinstance(visa_type, str) else [v for v in visa_type if v]
    if not has_any_country_expertise(profile, destinations):
        return 0.0
    if not has_any_visa_specialization(profile, visas):
        return 0.0

    score = float(weights.country) + float(weights.visa)
    score += _language_points(profile, preferred_languages, weights.language)
    score += _services_points(profile, needed_services, weights.services)
    score += _experience_points(profile.years_of_experience, weights.experience)
    score += _rating_points(average_rating, weights.rating)

    return round(min(score, 100.0), 2)


async def match_context_from_profile(
    session: AsyncSession, seeker_id: uuid.UUID
) -> tuple[str | None, str | None]:
    """Destination + visa from seeker profile intent only (Find Advisor)."""
    profile = (
        await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == seeker_id))
    ).scalar_one_or_none()
    if profile is None:
        return None, None
    return profile.intended_destination, profile.intended_visa_type


async def match_context_for_seeker(
    session: AsyncSession, seeker_id: uuid.UUID
) -> tuple[str | None, str | None]:
    """Destination + visa from profile intent only."""
    return await match_context_from_profile(session, seeker_id)


def _soft_fields_from_profile(profile: SeekerProfile | None) -> dict[str, object]:
    if profile is None:
        return {
            "preferred_languages": (),
            "needed_services": (),
            "timezone": None,
            "nationality": None,
            "country_of_residence": None,
            "annual_income_band": None,
        }
    return {
        "preferred_languages": tuple(preferred_language_names(profile)),
        "needed_services": tuple(needed_service_ids(profile)),
        "timezone": profile.timezone,
        "nationality": profile.nationality,
        "country_of_residence": profile.country_of_residence,
        "annual_income_band": profile.annual_income_band,
    }


async def build_profile_match_case(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    destination: str | None = None,
    visa_type: str | None = None,
) -> SeekerMatchCase | None:
    """Match case from seeker profile intent only — never uses AI Assessment."""
    profile = (
        await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == seeker_id))
    ).scalar_one_or_none()
    if profile is None:
        return None

    destinations = (
        [destination.upper()]
        if destination
        else intended_destination_codes(profile)
    )
    visas = [visa_type] if visa_type else intended_visa_type_values(profile)
    if not destinations or not visas:
        return None

    soft = _soft_fields_from_profile(profile)
    return SeekerMatchCase(
        destination_country=destinations[0],
        visa_type=visas[0],
        destination_countries=tuple(destinations),
        visa_types=tuple(visas),
        seeker_id=seeker_id,
        assessment_id=None,
        eligibility_tier=None,
        eligibility_score=None,
        preferred_languages=soft["preferred_languages"],  # type: ignore[arg-type]
        needed_services=soft["needed_services"],  # type: ignore[arg-type]
        timezone=soft["timezone"],  # type: ignore[arg-type]
        nationality=soft["nationality"],  # type: ignore[arg-type]
        country_of_residence=soft["country_of_residence"],  # type: ignore[arg-type]
        annual_income_band=soft["annual_income_band"],  # type: ignore[arg-type]
        context_source="profile",
    )


async def build_seeker_match_case(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    destination: str | None = None,
    visa_type: str | None = None,
) -> SeekerMatchCase | None:
    """Build a full match case from assessment (preferred) or profile intent."""
    profile = (
        await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == seeker_id))
    ).scalar_one_or_none()

    assessment = (
        await session.execute(
            select(Assessment)
            .where(
                Assessment.user_id == seeker_id,
                Assessment.status == AssessmentStatus.completed,
            )
            .order_by(Assessment.completed_at.desc().nulls_last(), Assessment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    soft = _soft_fields_from_profile(profile)

    dest = (destination or "").upper() or None
    visa = visa_type
    if assessment is not None and dest is None and visa is None:
        return case_from_assessment(
            assessment,
            preferred_languages=soft["preferred_languages"],  # type: ignore[arg-type]
            needed_services=soft["needed_services"],  # type: ignore[arg-type]
            timezone=soft["timezone"],  # type: ignore[arg-type]
            nationality=soft["nationality"],  # type: ignore[arg-type]
            country_of_residence=soft["country_of_residence"],  # type: ignore[arg-type]
            annual_income_band=soft["annual_income_band"],  # type: ignore[arg-type]
        )

    if dest is None:
        if assessment is not None:
            dest = assessment.destination_country.upper()
        elif profile and profile.intended_destination:
            dest = profile.intended_destination.upper()

    if visa is None:
        if assessment is not None:
            visa = assessment.visa_type
        elif profile and profile.intended_visa_type:
            visa = profile.intended_visa_type

    if not dest or not visa:
        return None

    if (
        assessment is not None
        and dest == assessment.destination_country.upper()
        and visa == assessment.visa_type
    ):
        return case_from_assessment(
            assessment,
            preferred_languages=soft["preferred_languages"],  # type: ignore[arg-type]
            needed_services=soft["needed_services"],  # type: ignore[arg-type]
            timezone=soft["timezone"],  # type: ignore[arg-type]
            nationality=soft["nationality"],  # type: ignore[arg-type]
            country_of_residence=soft["country_of_residence"],  # type: ignore[arg-type]
            annual_income_band=soft["annual_income_band"],  # type: ignore[arg-type]
        )

    return SeekerMatchCase(
        destination_country=dest,
        visa_type=visa,
        seeker_id=seeker_id,
        assessment_id=None,
        eligibility_tier=None,
        eligibility_score=None,
        preferred_languages=soft["preferred_languages"],  # type: ignore[arg-type]
        needed_services=soft["needed_services"],  # type: ignore[arg-type]
        timezone=soft["timezone"],  # type: ignore[arg-type]
        nationality=soft["nationality"],  # type: ignore[arg-type]
        country_of_residence=soft["country_of_residence"],  # type: ignore[arg-type]
        annual_income_band=soft["annual_income_band"],  # type: ignore[arg-type]
        context_source="profile",
    )


async def _seeker_soft_context(session: AsyncSession, seeker_id: uuid.UUID) -> SeekerMatchCase:
    """Soft-only profile fields (destination/visa placeholders unused by callers)."""
    profile = (
        await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == seeker_id))
    ).scalar_one_or_none()
    if profile is None:
        return SeekerMatchCase(destination_country="", visa_type="")
    soft = _soft_fields_from_profile(profile)
    return SeekerMatchCase(
        destination_country="",
        visa_type="",
        seeker_id=seeker_id,
        preferred_languages=soft["preferred_languages"],  # type: ignore[arg-type]
        needed_services=soft["needed_services"],  # type: ignore[arg-type]
        timezone=soft["timezone"],  # type: ignore[arg-type]
        nationality=soft["nationality"],  # type: ignore[arg-type]
        country_of_residence=soft["country_of_residence"],  # type: ignore[arg-type]
        annual_income_band=soft["annual_income_band"],  # type: ignore[arg-type]
        context_source="profile",
    )


def match_percentage(
    profile: AdvisorProfile | None,
    destination: str | None,
    visa_type: str | None,
    average_rating: float | None,
    *,
    weights: MatchingWeightConfig = DEFAULT_CONFIG,
    preferred_languages: list[str] | None = None,
    needed_services: list[str] | None = None,
) -> int | None:
    """0–100 match for seeker-facing advisor cards; ``None`` without destination/visa.

    Returns ``0`` when the advisor lacks destination-country expertise or the
    seeker's visa specialization.
    """
    if profile is None or not destination or not visa_type:
        return None
    return int(
        round(
            score_advisor_for_assessment(
                profile,
                destination,
                visa_type,
                average_rating,
                weights=weights,
                preferred_languages=preferred_languages,
                needed_services=needed_services,
            )
        )
    )


def _apply_ai_blend(
    rule_ranked: list[AdvisorMatchRead],
    ai_items: list[ai_advisor_match_service.AiRerankItem],
) -> list[AdvisorMatchRead]:
    """Blend AI re-rank into the top pool; keep remaining rule-only candidates after."""
    by_id = {m.user_id: m for m in rule_ranked}

    blended: list[AdvisorMatchRead] = []
    used: set[uuid.UUID] = set()
    for item in ai_items:
        base = by_id.get(item.advisor_id)
        if base is None:
            continue
        used.add(item.advisor_id)
        ai_score = item.ai_score if item.ai_score is not None else base.match_score
        blended.append(
            base.model_copy(
                update={
                    "rule_score": base.match_score,
                    "ai_score": ai_score,
                    "match_score": blend_scores(base.match_score, ai_score),
                    "match_reasons": item.reason,
                }
            )
        )

    rest = [
        m.model_copy(update={"rule_score": m.match_score, "ai_score": None, "match_reasons": None})
        for m in rule_ranked
        if m.user_id not in used
    ]
    blended.sort(key=lambda m: (m.match_score, m.average_rating or 0), reverse=True)
    rest.sort(key=lambda m: (m.match_score, m.average_rating or 0), reverse=True)
    return blended + rest


def _rule_only_matches(matches: list[AdvisorMatchRead]) -> list[AdvisorMatchRead]:
    return [
        m.model_copy(
            update={
                "rule_score": m.match_score,
                "ai_score": None,
                "match_reasons": None,
            }
        )
        for m in matches
    ]


async def match_from_context(
    session: AsyncSession,
    case: SeekerMatchCase,
    *,
    candidates: list[tuple[User, AdvisorProfile]] | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    positive_only: bool = True,
    settings: Settings | None = None,
    use_ai: bool = True,
) -> tuple[list[AdvisorMatchRead], int, AiMatchFailure | None]:
    """Rank advisors for a seeker case with rule scoring + optional OpenAI blend."""
    weights = await matching_weights_service.get_config(session)

    if candidates is None:
        destinations = list(case.destinations_for_gate())
        visas = list(case.visas_for_gate())
        stmt = (
            select(User, AdvisorProfile)
            .join(AdvisorProfile, AdvisorProfile.user_id == User.id)
            .join(
                AdvisorCountryExpertise,
                AdvisorCountryExpertise.profile_id == AdvisorProfile.id,
            )
            .join(
                AdvisorVisaSpecialization,
                AdvisorVisaSpecialization.profile_id == AdvisorProfile.id,
            )
            .where(User.role == UserRole.advisor)
            .where(User.is_active.is_(True))
            .where(User.verification_status == VerificationStatus.approved)
            .where(AdvisorCountryExpertise.country_code.in_(destinations))
            .where(AdvisorVisaSpecialization.specialization.in_(visas))
            .distinct()
        )
        stmt = apply_integrations_ready_filter(stmt)
        rows = [(user, profile) for user, profile in (await session.execute(stmt)).all()]
    else:
        rows = list(candidates)

    advisor_ids = [user.id for user, _ in rows]
    ratings = await review_service.rating_summaries(session, advisor_ids)

    gate_destinations = list(case.destinations_for_gate())
    gate_visas = list(case.visas_for_gate())

    matches: list[AdvisorMatchRead] = []
    for user, profile in rows:
        if not has_any_country_expertise(profile, gate_destinations):
            continue
        if not has_any_visa_specialization(profile, gate_visas):
            continue

        rating = ratings[user.id][0] if user.id in ratings else None
        score = score_advisor_for_assessment(
            profile,
            gate_destinations,
            gate_visas,
            rating,
            weights=weights,
            preferred_languages=list(case.preferred_languages),
            needed_services=list(case.needed_services),
        )
        matches.append(
            AdvisorMatchRead(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                title=profile.title,
                profile_photo_url=profile.profile_photo_url,
                years_of_experience=profile.years_of_experience,
                average_rating=rating,
                starting_price_usd=starting_price_usd(profile),
                match_score=score,
                public_profile_slug=profile.public_profile_slug,
                rule_score=None,
                ai_score=None,
                match_reasons=None,
                visa_specializations=[
                    s.specialization for s in (profile.visa_specializations or [])
                ],
                country_expertise=[c.country_code for c in (profile.country_expertise or [])],
            )
        )

    if positive_only:
        matches = [m for m in matches if m.match_score > 0]
    matches.sort(key=lambda m: (m.match_score, m.average_rating or 0), reverse=True)

    ai_failure: AiMatchFailure | None = None
    if use_ai and matches:
        cfg = settings or get_settings()
        outcome = await ai_advisor_match_service.rerank_advisors(
            case,
            matches[:AI_CANDIDATE_POOL],
            cfg,
        )
        if outcome.failure is not None:
            ai_failure = outcome.failure
            matches = _rule_only_matches(matches)
        else:
            matches = _apply_ai_blend(matches, outcome.items)

    total = len(matches)
    if limit <= 0:
        return [], total, ai_failure
    page = matches[offset : offset + limit]
    return page, total, ai_failure


async def match(
    session: AsyncSession,
    assessment: Assessment,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    positive_only: bool = True,
    settings: Settings | None = None,
    use_ai: bool = True,
) -> tuple[list[AdvisorMatchRead], int, AiMatchFailure | None]:
    """Rank advisors for a completed assessment (assessment-scoped soft fields)."""
    soft = await _seeker_soft_context(session, assessment.user_id)
    case = case_from_assessment(
        assessment,
        preferred_languages=soft.preferred_languages,
        needed_services=soft.needed_services,
        timezone=soft.timezone,
        nationality=soft.nationality,
        country_of_residence=soft.country_of_residence,
        annual_income_band=soft.annual_income_band,
    )
    return await match_from_context(
        session,
        case,
        limit=limit,
        offset=offset,
        positive_only=positive_only,
        settings=settings,
        use_ai=use_ai,
    )
