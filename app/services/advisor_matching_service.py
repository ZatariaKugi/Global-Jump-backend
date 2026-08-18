"""AI advisor matching — ranked shortlist for seekers (PRD §3.4.3).

Hybrid pipeline:
1. Hard gate — approved/active advisors with destination-country expertise.
   Visa-only matches (same visa, different country) are excluded.
2. Rule score — country + visa + language + availability + experience + price
   (+ rating on visa).
3. AI re-rank — OpenAI reorders the top rule-scored pool and blends scores.
   If OpenAI is unavailable, pure rule ranking is kept.

Ranking preference among eligible advisors:
1. Country + visa (best combo)
2. Country only (still eligible, lower score)
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.visa_types import parse_visa_type
from app.models.advisor_availability import AdvisorWeeklySlot
from app.models.advisor_profile import AdvisorProfile
from app.models.assessment import Assessment, AssessmentStatus
from app.models.seeker_profile import SeekerProfile
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.assessment import AdvisorMatchRead
from app.services import ai_advisor_match_service, matching_weights_service, review_service
from app.services.advisor_profile_service import starting_price_usd
from app.services.advisor_search_service import apply_integrations_ready_filter
from app.services.ai_advisor_match_service import (
    AI_CANDIDATE_POOL,
    SeekerMatchCase,
    blend_scores,
    case_from_assessment,
)
from app.services.matching_weights_service import DEFAULT_CONFIG, MatchingWeightConfig

DEFAULT_LIMIT = 5

# Soft bonuses beyond the admin weight config (kept small so country+visa still leads).
_EXPERIENCE_BONUS_MAX = 8.0
_PRICE_BONUS_MAX = 6.0


def has_country_expertise(profile: AdvisorProfile, destination: str) -> bool:
    """True when the advisor lists the seeker's destination country."""
    countries = {c.country_code.upper() for c in (profile.country_expertise or [])}
    return destination.upper() in countries


def has_visa_specialization(profile: AdvisorProfile, visa_type: str) -> bool:
    """True when the advisor specializes in the seeker's visa type."""
    specializations = {
        parsed
        for s in (profile.visa_specializations or [])
        if (parsed := parse_visa_type(s.specialization)) is not None
    }
    target = parse_visa_type(visa_type)
    return target is not None and target in specializations


def _language_points(
    profile: AdvisorProfile,
    preferred_language: str | None,
    weight: float,
) -> float:
    """PRD language factor: prefer real preference match over 'has any language'."""
    if not profile.languages:
        return 0.0
    if not preferred_language or not preferred_language.strip():
        # Seeker preference unknown — small credit for having languages configured.
        return weight * 0.5

    pref = preferred_language.strip().lower()
    advisor_langs = {lang.language.strip().lower() for lang in profile.languages if lang.language}
    if pref in advisor_langs:
        return weight
    # Soft contains match ("en" / "english").
    if any(pref in lang or lang in pref for lang in advisor_langs):
        return weight
    return 0.0


def _experience_points(years: int | None) -> float:
    """Bounded experience bonus — more years help, but never dominate country/visa."""
    if years is None or years <= 0:
        return 0.0
    # 0–5y: ramp to half; 5–15y: ramp to full; 15+: cap.
    if years >= 15:
        return _EXPERIENCE_BONUS_MAX
    if years >= 5:
        return _EXPERIENCE_BONUS_MAX * (0.5 + 0.5 * min((years - 5) / 10.0, 1.0))
    return _EXPERIENCE_BONUS_MAX * 0.5 * (years / 5.0)


def _income_midpoint(band: str | None) -> float | None:
    """Parse a rough USD midpoint from bands like ``$50,000–$100,000`` / ``100000-250000``."""
    if not band:
        return None
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", band)]
    if not nums:
        return None
    if len(nums) == 1:
        return float(nums[0])
    return (nums[0] + nums[1]) / 2.0


def _price_points(starting_price: float | None, income_band: str | None) -> float:
    """Soft price fit vs seeker income band — missing data yields a small neutral credit."""
    if starting_price is None:
        return _PRICE_BONUS_MAX * 0.25
    mid = _income_midpoint(income_band)
    if mid is None or mid <= 0:
        # No income context — prefer mid-range consultation prices.
        if 50 <= starting_price <= 300:
            return _PRICE_BONUS_MAX * 0.7
        if starting_price < 50 or starting_price <= 500:
            return _PRICE_BONUS_MAX * 0.4
        return 0.0
    # Rough affordability: consultation price as a tiny fraction of annual income.
    ratio = starting_price / mid
    if ratio <= 0.002:
        return _PRICE_BONUS_MAX
    if ratio <= 0.005:
        return _PRICE_BONUS_MAX * 0.75
    if ratio <= 0.01:
        return _PRICE_BONUS_MAX * 0.45
    if ratio <= 0.02:
        return _PRICE_BONUS_MAX * 0.2
    return 0.0


def score_advisor_for_assessment(
    profile: AdvisorProfile,
    destination: str,
    visa_type: str,
    average_rating: float | None,
    *,
    weights: MatchingWeightConfig = DEFAULT_CONFIG,
    has_availability: bool = False,
    preferred_language: str | None = None,
    annual_income_band: str | None = None,
) -> float:
    """Weighted match score for recommendations.

    Returns ``0`` when destination country is missing from the advisor's expertise
    (visa-only matches are never recommended).
    """
    if not has_country_expertise(profile, destination):
        return 0.0

    score = float(weights.country)
    score += _language_points(profile, preferred_language, weights.language)

    if has_availability:
        score += weights.availability

    # Visa points only on top of a country match (best combo).
    if has_visa_specialization(profile, visa_type):
        setting = weights.setting
        if average_rating is not None:
            score += setting * (0.7 + 0.3 * (average_rating / 5.0))
        else:
            score += setting * 0.7

    score += _experience_points(profile.years_of_experience)
    score += _price_points(starting_price_usd(profile), annual_income_band)

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
    """Destination + visa from profile intent only.

    Kept as an alias of ``match_context_from_profile`` so assessment country/visa
    never overrides Find Advisor, bookmarks, or the profile recommendation cache.
    Assessment matches stay on ``advisor_leads`` + live ``match(assessment)``.
    """
    return await match_context_from_profile(session, seeker_id)


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

    dest = (destination or profile.intended_destination or "").upper() or None
    visa = visa_type or profile.intended_visa_type
    if not dest or not visa:
        return None

    return SeekerMatchCase(
        destination_country=dest,
        visa_type=visa,
        seeker_id=seeker_id,
        assessment_id=None,
        eligibility_tier=None,
        eligibility_score=None,
        preferred_language=profile.preferred_language,
        timezone=profile.timezone,
        nationality=profile.nationality,
        country_of_residence=profile.country_of_residence,
        annual_income_band=profile.annual_income_band,
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

    soft = {
        "preferred_language": profile.preferred_language if profile else None,
        "timezone": profile.timezone if profile else None,
        "nationality": profile.nationality if profile else None,
        "country_of_residence": profile.country_of_residence if profile else None,
        "annual_income_band": profile.annual_income_band if profile else None,
    }

    dest = (destination or "").upper() or None
    visa = visa_type
    if assessment is not None and dest is None and visa is None:
        return case_from_assessment(assessment, **soft)

    if dest is None:
        if assessment is not None:
            dest = assessment.destination_country.upper()
        elif profile and profile.intended_destination:
            dest = profile.intended_destination.upper()
    if visa is None:
        if assessment is not None:
            visa = assessment.visa_type
        elif profile:
            visa = profile.intended_visa_type

    if not dest or not visa:
        return None

    if (
        assessment is not None
        and dest == assessment.destination_country.upper()
        and visa == (assessment.visa_type)
    ):
        return case_from_assessment(assessment, **soft)

    return SeekerMatchCase(
        destination_country=dest,
        visa_type=visa,
        seeker_id=seeker_id,
        assessment_id=None,
        eligibility_tier=None,
        eligibility_score=None,
        context_source="profile",
        **soft,
    )


async def _seeker_soft_context(session: AsyncSession, seeker_id: uuid.UUID) -> SeekerMatchCase:
    """Soft-only profile fields (destination/visa placeholders unused by callers)."""
    profile = (
        await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == seeker_id))
    ).scalar_one_or_none()
    if profile is None:
        return SeekerMatchCase(destination_country="", visa_type="")
    return SeekerMatchCase(
        destination_country="",
        visa_type="",
        seeker_id=seeker_id,
        preferred_language=profile.preferred_language,
        timezone=profile.timezone,
        nationality=profile.nationality,
        country_of_residence=profile.country_of_residence,
        annual_income_band=profile.annual_income_band,
        context_source="profile",
    )


def match_percentage(
    profile: AdvisorProfile | None,
    destination: str | None,
    visa_type: str | None,
    average_rating: float | None,
    *,
    weights: MatchingWeightConfig = DEFAULT_CONFIG,
    has_availability: bool = False,
    preferred_language: str | None = None,
    annual_income_band: str | None = None,
) -> int | None:
    """0–100 match for seeker-facing advisor cards; ``None`` without destination/visa.

    Returns ``0`` (not a recommendation) when the advisor lacks destination-country
    expertise — including visa-only overlaps. Card % uses the rule engine only
    when AI blend is not applied.
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
                has_availability=has_availability,
                preferred_language=preferred_language,
                annual_income_band=annual_income_band,
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
        blended.append(
            base.model_copy(
                update={
                    "rule_score": base.match_score,
                    "ai_score": item.ai_score,
                    "match_score": blend_scores(base.match_score, item.ai_score),
                    "match_reasons": item.reason,
                }
            )
        )

    # Preserve rule order for anyone outside the AI pool / omitted by the model.
    rest = [
        m.model_copy(update={"rule_score": m.match_score, "ai_score": None, "match_reasons": None})
        for m in rule_ranked
        if m.user_id not in used
    ]
    blended.sort(key=lambda m: (m.match_score, m.average_rating or 0), reverse=True)
    rest.sort(key=lambda m: (m.match_score, m.average_rating or 0), reverse=True)
    return blended + rest


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
) -> tuple[list[AdvisorMatchRead], int]:
    """Rank advisors for a seeker case with rule scoring + optional OpenAI blend.

    When ``candidates`` is provided (e.g. Find Advisor search filters), only that
    set is scored. Otherwise all approved, integration-ready advisors are loaded.
    """
    weights = await matching_weights_service.get_config(session)

    if candidates is None:
        stmt = (
            select(User, AdvisorProfile)
            .join(AdvisorProfile, AdvisorProfile.user_id == User.id)
            .where(User.role == UserRole.advisor)
            .where(User.is_active.is_(True))
            .where(User.verification_status == VerificationStatus.approved)
        )
        stmt = apply_integrations_ready_filter(stmt)
        rows = [(user, profile) for user, profile in (await session.execute(stmt)).all()]
    else:
        rows = list(candidates)

    advisor_ids = [user.id for user, _ in rows]
    ratings = await review_service.rating_summaries(session, advisor_ids)

    available: set[uuid.UUID] = set()
    if advisor_ids:
        slot_rows = (
            await session.execute(
                select(AdvisorWeeklySlot.advisor_id)
                .where(AdvisorWeeklySlot.advisor_id.in_(advisor_ids))
                .distinct()
            )
        ).all()
        available = {row[0] for row in slot_rows}

    matches: list[AdvisorMatchRead] = []
    for user, profile in rows:
        if not has_country_expertise(profile, case.destination_country):
            continue

        rating = ratings[user.id][0] if user.id in ratings else None
        score = score_advisor_for_assessment(
            profile,
            case.destination_country,
            case.visa_type,
            rating,
            weights=weights,
            has_availability=user.id in available,
            preferred_language=case.preferred_language,
            annual_income_band=case.annual_income_band,
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
            )
        )

    if positive_only:
        matches = [m for m in matches if m.match_score > 0]
    matches.sort(key=lambda m: (m.match_score, m.average_rating or 0), reverse=True)

    if use_ai and matches:
        cfg = settings or get_settings()
        ai_items = await ai_advisor_match_service.rerank_advisors(
            case,
            matches[:AI_CANDIDATE_POOL],
            cfg,
        )
        if ai_items is not None:
            matches = _apply_ai_blend(matches, ai_items)

    total = len(matches)
    if limit <= 0:
        return [], total
    page = matches[offset : offset + limit]
    return page, total


async def match(
    session: AsyncSession,
    assessment: Assessment,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    positive_only: bool = True,
    settings: Settings | None = None,
    use_ai: bool = True,
) -> tuple[list[AdvisorMatchRead], int]:
    """Rank advisors for a completed assessment (hybrid rule + optional AI)."""
    soft = await _seeker_soft_context(session, assessment.user_id)
    case = case_from_assessment(
        assessment,
        preferred_language=soft.preferred_language,
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
