"""Seeker bookmarks of advisors — create, list, delete, status check."""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from app.models.advisor_bookmark import AdvisorBookmark
from app.models.advisor_profile import AdvisorProfile, AdvisorVisaSpecialization
from app.models.user import User, UserRole, VerificationStatus
from app.models.visa_type import VisaType
from app.schemas.bookmark import BookmarkRead
from app.services import advisor_matching_service, advisor_search_service, review_service
from app.services.advisor_search_service import SortOption

_AdvisorStatus = Literal["active", "inactive"]


async def _require_approved_advisor(session: AsyncSession, advisor_id: uuid.UUID) -> User:
    advisor = await session.get(User, advisor_id)
    if (
        advisor is None
        or advisor.role != UserRole.advisor
        or not advisor.is_active
        or bool(getattr(advisor, "is_suspended", False))
        or advisor.verification_status != VerificationStatus.approved
    ):
        raise NotFoundError("Advisor not found")
    return advisor


async def create(
    session: AsyncSession, seeker: User, advisor_id: uuid.UUID
) -> AdvisorBookmark:
    if seeker.role != UserRole.seeker:
        raise PermissionDeniedError("Seeker account required")
    await _require_approved_advisor(session, advisor_id)

    existing = (
        await session.execute(
            select(AdvisorBookmark).where(
                AdvisorBookmark.seeker_id == seeker.id,
                AdvisorBookmark.advisor_id == advisor_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not existing.is_archived:
            raise AppError("Advisor is already bookmarked", code="already_bookmarked")
        existing.unarchive(seeker.id)
        session.add(existing)
        await session.flush()
        await session.refresh(existing)
        return existing

    bookmark = AdvisorBookmark(
        seeker_id=seeker.id,
        advisor_id=advisor_id,
        created_by=seeker.id,
    )
    session.add(bookmark)
    await session.flush()
    await session.refresh(bookmark)
    return bookmark


async def delete(
    session: AsyncSession, seeker: User, advisor_id: uuid.UUID
) -> None:
    if seeker.role != UserRole.seeker:
        raise PermissionDeniedError("Seeker account required")
    bookmark = (
        await session.execute(
            select(AdvisorBookmark).where(
                AdvisorBookmark.seeker_id == seeker.id,
                AdvisorBookmark.advisor_id == advisor_id,
                AdvisorBookmark.is_archived.is_(False),
            )
        )
    ).scalar_one_or_none()
    if bookmark is None:
        raise NotFoundError("Bookmark not found")
    bookmark.archive(seeker.id)
    session.add(bookmark)
    await session.flush()


async def is_bookmarked(
    session: AsyncSession, seeker_id: uuid.UUID, advisor_id: uuid.UUID
) -> bool:
    result = await session.execute(
        select(AdvisorBookmark.id).where(
            AdvisorBookmark.seeker_id == seeker_id,
            AdvisorBookmark.advisor_id == advisor_id,
            AdvisorBookmark.is_archived.is_(False),
        )
    )
    return result.scalar_one_or_none() is not None


async def bookmarked_advisor_ids(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    advisor_ids: list[uuid.UUID],
) -> set[uuid.UUID]:
    """Bulk membership check — which of ``advisor_ids`` the seeker has bookmarked."""
    if not advisor_ids:
        return set()
    rows = (
        await session.execute(
            select(AdvisorBookmark.advisor_id).where(
                AdvisorBookmark.seeker_id == seeker_id,
                AdvisorBookmark.advisor_id.in_(advisor_ids),
                AdvisorBookmark.is_archived.is_(False),
            )
        )
    ).scalars().all()
    return set(rows)


def list_for_seeker_stmt(
    seeker_id: uuid.UUID,
    *,
    q: str | None = None,
    visa_type: VisaType | None = None,
    sort: SortOption = "newest",
    recommended: bool = False,
) -> Select[tuple[AdvisorBookmark]]:
    stmt = (
        select(AdvisorBookmark)
        .join(User, User.id == AdvisorBookmark.advisor_id)
        .outerjoin(AdvisorProfile, AdvisorProfile.user_id == User.id)
        .where(AdvisorBookmark.seeker_id == seeker_id)
        .where(AdvisorBookmark.is_archived.is_(False))
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(pattern),
                User.email.ilike(pattern),
                AdvisorProfile.title.ilike(pattern),
            )
        )
    if visa_type is not None:
        stmt = stmt.where(
            exists().where(
                AdvisorVisaSpecialization.profile_id == AdvisorProfile.id,
                func.lower(AdvisorVisaSpecialization.specialization) == visa_type.value,
            )
        )
    return stmt.order_by(
        *advisor_search_service.advisor_order_by(sort=sort, recommended=recommended)
    )


def _advisor_status(advisor: User) -> _AdvisorStatus:
    if (
        advisor.is_active
        and not bool(getattr(advisor, "is_suspended", False))
        and advisor.verification_status == VerificationStatus.approved
    ):
        return "active"
    return "inactive"


async def build_list_reads(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    bookmarks: list[AdvisorBookmark],
) -> list[BookmarkRead]:
    if not bookmarks:
        return []

    advisor_ids = [b.advisor_id for b in bookmarks]
    advisors = {
        u.id: u
        for u in (
            await session.execute(select(User).where(User.id.in_(advisor_ids)))
        ).scalars().all()
    }
    profiles = {
        p.user_id: p
        for p in (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id.in_(advisor_ids))
            )
        ).scalars().all()
    }
    ratings = await review_service.rating_summaries(session, advisor_ids)
    destination, visa_type = await advisor_matching_service.match_context_for_seeker(
        session, seeker_id
    )

    out: list[BookmarkRead] = []
    for bookmark in bookmarks:
        advisor = advisors.get(bookmark.advisor_id)
        if advisor is None:
            continue
        profile = profiles.get(bookmark.advisor_id)
        avg, _count = ratings.get(bookmark.advisor_id, (None, 0))
        match_percentage = advisor_matching_service.match_percentage(
            profile, destination, visa_type, avg
        )

        expertise = None
        if profile is not None:
            expertise = profile.title
            if not expertise and profile.visa_specializations:
                expertise = profile.visa_specializations[0].specialization

        out.append(
            BookmarkRead(
                id=bookmark.id,
                advisor_id=advisor.id,
                full_name=advisor.full_name,
                email=advisor.email,
                profile_photo_url=profile.profile_photo_url if profile else None,
                expertise=expertise,
                average_rating=avg,
                years_of_experience=profile.years_of_experience if profile else None,
                match_percentage=match_percentage,
                status=_advisor_status(advisor),
                public_profile_slug=profile.public_profile_slug if profile else None,
                is_bookmarked=True,
                bookmarked_at=bookmark.created_at,
            )
        )
    return out
