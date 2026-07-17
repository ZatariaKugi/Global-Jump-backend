"""Admin "Verification Queue" — a global, cross-advisor list of advisors with
at least one pending AdvisorCredential, grouped one row per advisor."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.advisor_credential import AdvisorCredential, CredentialStatus, DocumentType
from app.models.advisor_profile import AdvisorProfile
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.advisor_admin import VerificationQueueRead

# Onboarding "Verification Documents" required types (PRD / wizard step 6).
_REQUIRED_DOC_TYPES = frozenset(
    {
        DocumentType.government_id,
        DocumentType.license,
        DocumentType.certification,
    }
)

_VerificationResult = Literal["all_passed", "needs_review"]
_QueueStatus = Literal["pending", "verified", "rejected"]


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
        # Account already decided → leave the queue even if a credential was
        # left pending by an older approve path.
        .where(
            User.verification_status.in_(
                (VerificationStatus.pending, VerificationStatus.under_review)
            )
        )
        .where(
            User.id.in_(
                select(AdvisorCredential.user_id)
                .where(AdvisorCredential.status == CredentialStatus.pending)
                .distinct()
            )
        )
        .order_by(earliest_pending.asc())
    )


def _normalize_doc_type(doc_type: DocumentType) -> DocumentType:
    if doc_type == DocumentType.immigration_license:
        return DocumentType.license
    return doc_type


def _package_score(doc_types: set[DocumentType]) -> tuple[float, _VerificationResult]:
    """0–100 completeness over the three required onboarding document types."""
    present = {_normalize_doc_type(t) for t in doc_types} & _REQUIRED_DOC_TYPES
    score = round((len(present) / len(_REQUIRED_DOC_TYPES)) * 100, 1)
    result: _VerificationResult = (
        "all_passed" if present == _REQUIRED_DOC_TYPES else "needs_review"
    )
    return score, result


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

    # All non-archived credentials (any status) for package-completeness score.
    cred_rows = (
        await session.execute(
            select(AdvisorCredential.user_id, AdvisorCredential.document_type)
            .where(AdvisorCredential.user_id.in_(ids))
            .where(AdvisorCredential.is_archived.is_(False))
        )
    ).all()
    types_by_advisor: dict[uuid.UUID, set[DocumentType]] = {i: set() for i in ids}
    for advisor_id, doc_type in cred_rows:
        types_by_advisor[advisor_id].add(doc_type)

    out = []
    for a in advisors:
        count, earliest, latest = stats.get(a.id, (0, a.created_at, a.created_at))
        ai_score, verification_result = _package_score(types_by_advisor.get(a.id, set()))
        status: _QueueStatus = "pending"
        out.append(
            VerificationQueueRead(
                advisor_id=a.id,
                full_name=a.full_name,
                email=a.email,
                profile_photo_url=photos.get(a.id),
                pending_document_count=count,
                earliest_submitted_at=earliest,
                latest_submitted_at=latest,
                ai_score=ai_score,
                verification_result=verification_result,
                status=status,
            )
        )
    return out
