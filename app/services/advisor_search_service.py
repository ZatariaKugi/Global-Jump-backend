"""Advisor discovery — search, filtering, and sorting query builders (PRD §3.5)."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import ScalarSelect, Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.advisor_profile import (
    AdvisorCountryExpertise,
    AdvisorLanguage,
    AdvisorProfile,
    AdvisorService,
    AdvisorVisaSpecialization,
)
from app.models.review import ModerationStatus, Review
from app.models.user import User, UserRole, VerificationStatus
from app.models.visa_type import VisaType

SortOption = Literal["newest", "price_asc", "price_desc", "experience", "rating", "review_count"]


@dataclass(slots=True)
class AdvisorSearchFilters:
    q: str | None = None
    country: str | None = None
    visa_type: VisaType | None = None
    language: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    min_rating: float | None = None
    featured_only: bool = False
    recommended: bool = False
    sort: SortOption = "newest"


def _min_price_subquery() -> ScalarSelect[Any]:
    """Correlated scalar subquery: cheapest service offered by the advisor."""
    return (
        select(func.min(AdvisorService.price_usd))
        .where(AdvisorService.profile_id == AdvisorProfile.id)
        .correlate(AdvisorProfile)
        .scalar_subquery()
    )


_PUBLIC_REVIEWS = (ModerationStatus.visible, ModerationStatus.flagged)


def _avg_rating_subquery() -> ScalarSelect[Any]:
    """Correlated scalar subquery: advisor's average public review rating."""
    return (
        select(func.avg(Review.rating_overall))
        .where(Review.advisor_id == User.id)
        .where(Review.moderation_status.in_(_PUBLIC_REVIEWS))
        .correlate(User)
        .scalar_subquery()
    )


def _review_count_subquery() -> ScalarSelect[Any]:
    """Correlated scalar subquery: advisor's public review count."""
    return (
        select(func.count(Review.id))
        .where(Review.advisor_id == User.id)
        .where(Review.moderation_status.in_(_PUBLIC_REVIEWS))
        .correlate(User)
        .scalar_subquery()
    )


def apply_integrations_ready_filter(
    stmt: Select[Any],
    *,
    require_zoom: bool | None = None,
) -> Select[Any]:
    """Hide advisors missing Stripe Connect readiness (and Zoom when configured).

    Requires ``User`` and ``AdvisorProfile`` in the statement's FROM/JOIN.
    When Zoom OAuth is not configured, the Zoom gate is skipped so local/dev
    environments without Zoom credentials still show the marketplace.
    """
    if require_zoom is None:
        require_zoom = get_settings().zoom_oauth_enabled
    stmt = stmt.where(AdvisorProfile.needs_stripe_connect.is_(False))
    if require_zoom:
        stmt = stmt.where(AdvisorProfile.needs_zoom_connect.is_(False))
    return stmt


async def is_integrations_ready(
    session: AsyncSession,
    advisor_user_id: uuid.UUID,
    *,
    require_zoom: bool | None = None,
) -> bool:
    """True when Stripe payouts are ready (and Zoom connected when configured)."""
    if require_zoom is None:
        require_zoom = get_settings().zoom_oauth_enabled
    profile = (
        await session.execute(
            select(AdvisorProfile).where(AdvisorProfile.user_id == advisor_user_id)
        )
    ).scalar_one_or_none()
    if profile is None or profile.needs_stripe_connect:
        return False
    return not (require_zoom and profile.needs_zoom_connect)


def build_search_stmt(filters: AdvisorSearchFilters) -> Select[tuple[User]]:
    """Build a ``User`` select for approved advisors matching the given filters.

    All multi-valued filters run as ``EXISTS`` subqueries against the normalised
    child tables (no JSON columns). Public discovery also requires Stripe, and
    Zoom when Zoom OAuth is configured.
    """
    stmt = (
        select(User)
        .outerjoin(AdvisorProfile, AdvisorProfile.user_id == User.id)
        .where(User.role == UserRole.advisor)
        .where(User.is_active.is_(True))
        .where(User.verification_status == VerificationStatus.approved)
    )
    stmt = apply_integrations_ready_filter(stmt)

    if filters.q:
        pattern = f"%{filters.q.strip()}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(pattern),
                AdvisorProfile.title.ilike(pattern),
                AdvisorProfile.bio.ilike(pattern),
                exists().where(
                    AdvisorVisaSpecialization.profile_id == AdvisorProfile.id,
                    AdvisorVisaSpecialization.specialization.ilike(pattern),
                ),
            )
        )

    if filters.country:
        stmt = stmt.where(
            exists().where(
                AdvisorCountryExpertise.profile_id == AdvisorProfile.id,
                func.upper(AdvisorCountryExpertise.country_code) == filters.country.upper(),
            )
        )

    if filters.visa_type:
        stmt = stmt.where(
            exists().where(
                AdvisorVisaSpecialization.profile_id == AdvisorProfile.id,
                func.lower(AdvisorVisaSpecialization.specialization) == filters.visa_type.value,
            )
        )

    if filters.language:
        stmt = stmt.where(
            exists().where(
                AdvisorLanguage.profile_id == AdvisorProfile.id,
                func.lower(AdvisorLanguage.language) == filters.language.lower(),
            )
        )

    if filters.min_price is not None or filters.max_price is not None:
        price_clauses = [AdvisorService.profile_id == AdvisorProfile.id]
        if filters.min_price is not None:
            price_clauses.append(AdvisorService.price_usd >= filters.min_price)
        if filters.max_price is not None:
            price_clauses.append(AdvisorService.price_usd <= filters.max_price)
        stmt = stmt.where(exists().where(*price_clauses))

    if filters.min_rating is not None:
        stmt = stmt.where(_avg_rating_subquery() >= filters.min_rating)

    if filters.featured_only:
        stmt = stmt.where(AdvisorProfile.is_featured.is_(True))

    return stmt.order_by(*advisor_order_by(sort=filters.sort, recommended=filters.recommended))


def advisor_order_by(
    *,
    sort: SortOption = "newest",
    recommended: bool = False,
) -> list[Any]:
    """Shared sort clauses for advisor discovery and bookmarked-advisor lists.

    Requires ``User`` and ``AdvisorProfile`` to be in the FROM/JOIN clause
    (``AdvisorProfile`` may be OUTER JOIN'd).
    """
    order_by: list[Any] = []
    if recommended:
        # Prefer featured when no seeker match context; list_advisors re-sorts by
        # AI match_percentage when destination/visa are known.
        order_by.append(func.coalesce(AdvisorProfile.is_featured, False).desc())

    if sort == "price_asc":
        order_by.append(_min_price_subquery().asc().nulls_last())
    elif sort == "price_desc":
        order_by.append(_min_price_subquery().desc().nulls_last())
    elif sort == "experience":
        order_by.append(AdvisorProfile.years_of_experience.desc().nulls_last())
    elif sort == "rating":
        order_by.append(_avg_rating_subquery().desc().nulls_last())
    elif sort == "review_count":
        order_by.append(_review_count_subquery().desc())
    else:  # newest
        order_by.append(User.created_at.desc())

    return order_by


# ── Public profile slug ──────────────────────────────────────────────────────

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SLUG_INVALID.sub("-", value.lower()).strip("-")[:80] or "advisor"


async def generate_unique_slug(session: AsyncSession, full_name: str | None) -> str:
    """Slugify the advisor's name, adding a short random suffix on collision."""
    base = slugify(full_name or "advisor")
    slug = base
    while True:
        result = await session.execute(
            select(AdvisorProfile.id).where(AdvisorProfile.public_profile_slug == slug)
        )
        if result.scalar_one_or_none() is None:
            return slug
        slug = f"{base}-{secrets.token_hex(3)}"
