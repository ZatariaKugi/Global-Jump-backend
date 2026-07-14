"""Admin "Verification Queue" — a global, cross-advisor list of advisors with
at least one pending AdvisorCredential, grouped one row per advisor."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.advisor_credential import AdvisorCredential, CredentialStatus
from app.models.advisor_profile import AdvisorProfile
from app.models.user import User, UserRole
from app.schemas.advisor_admin import VerificationQueueRead


def list_stmt() -> Select[tuple[User]]:
    """One row per advisor with >=1 pending credential.

    Stays a bare select(User) so app.api.pagination.paginate() (single-entity
    only) works unmodified — membership filtered via WHERE User.id IN
    (subquery), ordering via a correlated MIN(created_at) scalar subquery
    (same shape as advisor_search_service._avg_rating_subquery, used in
    .order_by() over a plain select(User)). Per-page count/date enrichment
    happens separately in build_list_read (fetch-page-then-bulk-enrich).
    """
    earliest_pending = (
        select(func.min(AdvisorCredential.created_at))
        .where(AdvisorCredential.user_id == User.id)
        .where(AdvisorCredential.status == CredentialStatus.pending)
        .correlate(User)
        .scalar_subquery()
    )
    return (
        select(User)
        .where(User.role == UserRole.advisor)
        .where(
            User.id.in_(
                select(AdvisorCredential.user_id)
                .where(AdvisorCredential.status == CredentialStatus.pending)
                .distinct()
            )
        )
        .order_by(earliest_pending.asc())
    )


async def build_list_read(
    session: AsyncSession, advisors: list[User]
) -> list[VerificationQueueRead]:
    ids = [a.id for a in advisors]
    if not ids:
        return []
    profile_rows = (
        (await session.execute(select(AdvisorProfile).where(AdvisorProfile.user_id.in_(ids))))
        .scalars()
        .all()
    )
    photos = {p.user_id: p.profile_photo_url for p in profile_rows}

    agg_rows = (
        await session.execute(
            select(
                AdvisorCredential.user_id,
                func.count(AdvisorCredential.id),
                func.min(AdvisorCredential.created_at),
                func.max(AdvisorCredential.created_at),
            )
            .where(AdvisorCredential.user_id.in_(ids))
            .where(AdvisorCredential.status == CredentialStatus.pending)
            .group_by(AdvisorCredential.user_id)
        )
    ).all()
    stats: dict[uuid.UUID, tuple[int, datetime, datetime]] = {}
    for advisor_id, count, earliest, latest in agg_rows:
        stats[advisor_id] = (count, earliest, latest)

    out = []
    for a in advisors:
        count, earliest, latest = stats.get(a.id, (0, a.created_at, a.created_at))
        out.append(
            VerificationQueueRead(
                advisor_id=a.id,
                full_name=a.full_name,
                email=a.email,
                profile_photo_url=photos.get(a.id),
                pending_document_count=count,
                earliest_submitted_at=earliest,
                latest_submitted_at=latest,
            )
        )
    return out
