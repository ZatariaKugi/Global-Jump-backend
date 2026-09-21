"""Seeker document portfolio — upload, listing, review, and comment thread."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import ColumnElement, Select, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.file_storage import (
    assert_safe_file_name,
    delete_file,
    resolve_media_url,
    resolve_url,
)
from app.core.visa_types import parse_visa_type
from app.models.booking import Booking
from app.models.notification import NotificationEntityType, NotificationType
from app.models.seeker_document import (
    DocumentCategory,
    SeekerDocument,
    SeekerDocumentAdvisorReview,
    SeekerDocumentComment,
    SeekerDocumentStatus,
)
from app.models.user import User, UserRole
from app.models.visa_type import VisaType
from app.schemas.booking import BookingSort
from app.schemas.seeker_document import (
    ChecklistItemStatus,
    ClientSeekerBrief,
    CustomerDocumentsRowRead,
    CustomerDocumentsRowStatus,
    DocumentChecklistItem,
    DocumentCommentAuthorRole,
    DocumentCommentRead,
    DocumentPortfolioSummary,
    SeekerDocumentCreate,
    SeekerDocumentRead,
    SeekerDocumentStatusUpdate,
    SeekerDocumentUpdate,
)
from app.services import booking_service, notification_service
from app.services.availability_service import as_utc

# Required portfolio categories — left-to-right tab order on the Documents page.
REQUIRED_CHECKLIST: tuple[DocumentCategory, ...] = (
    DocumentCategory.passport,
    DocumentCategory.educational,
    DocumentCategory.finance,
    DocumentCategory.supporting,
    DocumentCategory.other,
)
CHECKLIST_LABELS: dict[DocumentCategory, str] = {
    DocumentCategory.passport: "Passport",
    DocumentCategory.educational: "Educational",
    DocumentCategory.finance: "Finance",
    DocumentCategory.supporting: "Supporting",
    DocumentCategory.other: "Other",
}


async def create(
    session: AsyncSession, seeker_id: uuid.UUID, data: SeekerDocumentCreate, file_url: str
) -> SeekerDocument:
    assert_safe_file_name(data.file_name)
    assert_safe_file_name(data.document_name, field="Document name")
    document = SeekerDocument(
        seeker_id=seeker_id,
        category=data.category,
        document_name=data.document_name,
        file_url=file_url,
        file_size_bytes=data.file_size_bytes,
        content_type=data.content_type,
        expires_at=data.expires_at,
        visa_type=data.visa_type.value if data.visa_type is not None else None,
        created_by=seeker_id,
    )
    session.add(document)
    await session.flush()
    await session.refresh(document)
    return document


async def update_document(
    session: AsyncSession,
    document: SeekerDocument,
    data: SeekerDocumentUpdate,
    actor_id: uuid.UUID,
    *,
    file_url: str | None = None,
    settings: Settings | None = None,
) -> SeekerDocument:
    old_file_url = document.file_url
    if data.file_name is not None:
        assert_safe_file_name(data.file_name)
    if data.document_name is not None:
        assert_safe_file_name(data.document_name, field="Document name")
        document.document_name = data.document_name
    if data.clear_expires_at:
        document.expires_at = None
    elif data.expires_at is not None:
        document.expires_at = data.expires_at
        if document.status == SeekerDocumentStatus.expired:
            # Seeker set a new future expiry without re-uploading (or before
            # the file-replace block runs below).
            document.status = SeekerDocumentStatus.under_review
            document.reviewed_at = None
            document.reviewed_by = None
            await clear_advisor_reviews(session, document.id)
    if data.clear_visa_type:
        document.visa_type = None
    elif data.visa_type is not None:
        document.visa_type = data.visa_type.value
    if file_url is not None:
        document.file_url = file_url
        document.file_size_bytes = data.file_size_bytes or 0
        document.content_type = data.content_type or "application/octet-stream"
        # Same id, new file — send it back to review; keep the comment thread.
        document.status = SeekerDocumentStatus.under_review
        document.reviewed_at = None
        document.reviewed_by = None
        await clear_advisor_reviews(session, document.id)
        # A past expires_at would immediately flip the row back to expired (and the
        # FE treats past expiry as expired even when status is under_review).
        if (
            data.expires_at is None
            and not data.clear_expires_at
            and document.expires_at is not None
            and document.expires_at <= date.today()
        ):
            document.expires_at = None
    document.updated_by = actor_id
    session.add(document)
    await session.flush()
    await session.refresh(document)
    if (
        file_url is not None
        and settings is not None
        and old_file_url
        and old_file_url != document.file_url
        and old_file_url.startswith("/uploads/")
    ):
        delete_file(old_file_url, settings)
    return document


async def archive_document(
    session: AsyncSession, document: SeekerDocument, actor_id: uuid.UUID
) -> None:
    document.archive(actor_id)
    session.add(document)
    await session.flush()


def list_by_seeker_stmt(
    seeker_id: uuid.UUID,
    *,
    category: DocumentCategory | None = None,
    status: SeekerDocumentStatus | None = None,
    visa_type: VisaType | None = None,
    expiring_within_days: int | None = None,
    expires_before: date | None = None,
) -> Select[tuple[SeekerDocument]]:
    stmt = (
        select(SeekerDocument)
        .where(SeekerDocument.seeker_id == seeker_id)
        .where(SeekerDocument.is_archived.is_(False))
    )
    if category is not None:
        stmt = stmt.where(SeekerDocument.category == category)
    if status is not None:
        stmt = stmt.where(SeekerDocument.status == status)
    if visa_type is not None:
        # Untagged docs apply to every visa filter.
        stmt = stmt.where(
            or_(
                SeekerDocument.visa_type == visa_type.value,
                SeekerDocument.visa_type.is_(None),
            )
        )
    if expiring_within_days is not None:
        today = date.today()
        cutoff = today + timedelta(days=expiring_within_days)
        # Upcoming Expires: future-only (today through today+N). Past dates are
        # ``expired``, not upcoming.
        stmt = (
            stmt.where(SeekerDocument.expires_at.is_not(None))
            .where(SeekerDocument.expires_at >= today)
            .where(SeekerDocument.expires_at <= cutoff)
        )
    if expires_before is not None:
        stmt = stmt.where(SeekerDocument.expires_at.is_not(None)).where(
            SeekerDocument.expires_at <= expires_before
        )
    return stmt.order_by(SeekerDocument.created_at.desc())


async def refresh_expired_statuses(session: AsyncSession, seeker_id: uuid.UUID) -> None:
    """Persist ``expired`` when ``expires_at`` is in the past.

    Rejected always wins — a rejected file that has also lapsed stays rejected
    so Replace/review still target the rejection.
    """
    today = date.today()
    result = await session.execute(
        select(SeekerDocument).where(
            SeekerDocument.seeker_id == seeker_id,
            SeekerDocument.is_archived.is_(False),
            SeekerDocument.expires_at.is_not(None),
            SeekerDocument.expires_at < today,
            SeekerDocument.status.notin_(
                (SeekerDocumentStatus.rejected, SeekerDocumentStatus.expired)
            ),
        )
    )
    changed = False
    for doc in result.scalars():
        doc.status = SeekerDocumentStatus.expired
        session.add(doc)
        changed = True
    if changed:
        await session.flush()


async def get_for_seeker(
    session: AsyncSession, document_id: uuid.UUID, seeker_id: uuid.UUID
) -> SeekerDocument:
    await refresh_expired_statuses(session, seeker_id)
    document = await session.get(SeekerDocument, document_id)
    if document is None or document.is_archived:
        raise NotFoundError("Document not found")
    if document.seeker_id != seeker_id:
        raise NotFoundError("Document does not belong to this user")
    return document


async def get_by_id(session: AsyncSession, document_id: uuid.UUID) -> SeekerDocument:
    document = await session.get(SeekerDocument, document_id)
    if document is None or document.is_archived:
        raise NotFoundError("Document not found")
    await refresh_expired_statuses(session, document.seeker_id)
    await session.refresh(document)
    return document


async def set_status(
    session: AsyncSession,
    document: SeekerDocument,
    status: SeekerDocumentStatusUpdate,
    reviewer_id: uuid.UUID,
) -> SeekerDocument:
    """Admin/global review — mutates the document row."""
    document.status = status.status
    document.reviewed_at = datetime.now(UTC)
    document.reviewed_by = reviewer_id
    document.updated_by = reviewer_id
    session.add(document)
    await session.flush()
    await session.refresh(document)
    return document


def _review_stale_after_document_update(
    document: SeekerDocument,
    review: SeekerDocumentAdvisorReview,
) -> bool:
    """True when the file/metadata changed after this review (e.g. seeker re-uploaded)."""
    if document.status != SeekerDocumentStatus.under_review:
        return False
    if document.updated_at is None:
        return False
    # Allow a small clock/skew window so a fresh approve/reject is not treated as stale.
    return review.reviewed_at < document.updated_at - timedelta(seconds=2)


def advisor_effective_status(
    document: SeekerDocument,
    review: SeekerDocumentAdvisorReview | None,
) -> SeekerDocumentStatus:
    """Status shown to a specific advisor (isolated from other advisors' reviews)."""
    if document.status == SeekerDocumentStatus.expired:
        return SeekerDocumentStatus.expired
    if review is None:
        return SeekerDocumentStatus.under_review
    if _review_stale_after_document_update(document, review):
        return SeekerDocumentStatus.under_review
    return review.status


def seeker_effective_status(
    document: SeekerDocument,
    reviews: list[SeekerDocumentAdvisorReview],
) -> SeekerDocumentStatus:
    """Status shown to the seeker — from advisor review rows only (not stale global)."""
    if document.status == SeekerDocumentStatus.expired:
        return SeekerDocumentStatus.expired
    active = [r for r in reviews if not _review_stale_after_document_update(document, r)]
    if any(r.status == SeekerDocumentStatus.rejected for r in active):
        return SeekerDocumentStatus.rejected
    if any(r.status == SeekerDocumentStatus.approved for r in active):
        return SeekerDocumentStatus.approved
    return SeekerDocumentStatus.under_review


def _latest_review(
    reviews: list[SeekerDocumentAdvisorReview],
) -> SeekerDocumentAdvisorReview | None:
    if not reviews:
        return None
    return max(reviews, key=lambda r: r.reviewed_at)


async def reviews_by_document(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[SeekerDocumentAdvisorReview]]:
    if not document_ids:
        return {}
    rows = (
        await session.execute(
            select(SeekerDocumentAdvisorReview).where(
                SeekerDocumentAdvisorReview.document_id.in_(document_ids),
                SeekerDocumentAdvisorReview.is_archived.is_(False),
            )
        )
    ).scalars()
    grouped: dict[uuid.UUID, list[SeekerDocumentAdvisorReview]] = defaultdict(list)
    for row in rows:
        grouped[row.document_id].append(row)
    return grouped


async def reviews_for_advisor(
    session: AsyncSession,
    document_ids: list[uuid.UUID],
    advisor_id: uuid.UUID,
) -> dict[uuid.UUID, SeekerDocumentAdvisorReview]:
    if not document_ids:
        return {}
    rows = (
        await session.execute(
            select(SeekerDocumentAdvisorReview).where(
                SeekerDocumentAdvisorReview.document_id.in_(document_ids),
                SeekerDocumentAdvisorReview.advisor_id == advisor_id,
                SeekerDocumentAdvisorReview.is_archived.is_(False),
            )
        )
    ).scalars()
    return {row.document_id: row for row in rows}


async def clear_advisor_reviews(session: AsyncSession, document_id: uuid.UUID) -> None:
    """Drop all advisor review rows for a document (file replace / reset to review)."""
    await session.execute(
        delete(SeekerDocumentAdvisorReview).where(
            SeekerDocumentAdvisorReview.document_id == document_id,
        )
    )
    await session.flush()


async def set_advisor_review(
    session: AsyncSession,
    document: SeekerDocument,
    status: SeekerDocumentStatusUpdate,
    advisor_id: uuid.UUID,
) -> SeekerDocumentAdvisorReview | None:
    """Record this advisor's decision without changing global document status."""
    now = datetime.now(UTC)
    if status.status == SeekerDocumentStatus.under_review:
        await clear_advisor_reviews(session, document.id)
        return None

    existing = (
        await session.execute(
            select(SeekerDocumentAdvisorReview).where(
                SeekerDocumentAdvisorReview.document_id == document.id,
                SeekerDocumentAdvisorReview.advisor_id == advisor_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        review = SeekerDocumentAdvisorReview(
            document_id=document.id,
            advisor_id=advisor_id,
            status=status.status,
            reviewed_at=now,
            note=status.note,
            created_by=advisor_id,
            updated_by=advisor_id,
        )
    else:
        review = existing
        if review.is_archived:
            review.unarchive(advisor_id)
        review.status = status.status
        review.reviewed_at = now
        review.note = status.note
        review.updated_by = advisor_id
    session.add(review)
    document.updated_by = advisor_id
    session.add(document)
    await session.flush()
    await session.refresh(review)
    await session.refresh(document)
    return review


async def is_advisor_portfolio_completed(
    session: AsyncSession, seeker_id: uuid.UUID, advisor_id: uuid.UUID
) -> bool:
    """True when the seeker has ≥1 active doc and this advisor approved every one."""
    docs = list(
        (
            await session.execute(
                select(SeekerDocument).where(
                    SeekerDocument.seeker_id == seeker_id,
                    SeekerDocument.is_archived.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    if not docs:
        return False
    reviews = await reviews_for_advisor(session, [d.id for d in docs], advisor_id)
    return all(
        advisor_effective_status(doc, reviews.get(doc.id)) == SeekerDocumentStatus.approved
        for doc in docs
    )


async def assert_advisor_portfolio_editable(
    session: AsyncSession, seeker_id: uuid.UUID, advisor_id: uuid.UUID
) -> None:
    """Block advisor review mutations once they have approved every document."""
    if await is_advisor_portfolio_completed(session, seeker_id, advisor_id):
        raise ConflictError(
            "All documents are approved; this portfolio is locked",
            code="portfolio_completed",
        )


async def is_portfolio_completed(session: AsyncSession, seeker_id: uuid.UUID) -> bool:
    """True when the seeker has ≥1 active doc and every doc is approved."""
    rows = list(
        (
            await session.execute(
                select(SeekerDocument.status).where(
                    SeekerDocument.seeker_id == seeker_id,
                    SeekerDocument.is_archived.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    return bool(rows) and all(s == SeekerDocumentStatus.approved for s in rows)


async def assert_portfolio_editable(session: AsyncSession, seeker_id: uuid.UUID) -> None:
    """Block review mutations once the portfolio is fully approved (completed)."""
    if await is_portfolio_completed(session, seeker_id):
        raise ConflictError(
            "All documents are approved; this portfolio is locked",
            code="portfolio_completed",
        )


def _checklist_status_for_docs(
    docs: list[SeekerDocument],
    reviews_by_doc: dict[uuid.UUID, list[SeekerDocumentAdvisorReview]],
) -> tuple[ChecklistItemStatus, uuid.UUID | None]:
    """Pick the best status for a required category from matching uploads."""
    if not docs:
        return "missing", None
    approved = next(
        (
            d
            for d in docs
            if seeker_effective_status(d, reviews_by_doc.get(d.id, []))
            == SeekerDocumentStatus.approved
        ),
        None,
    )
    if approved is not None:
        return "approved", approved.id
    reviewing = next(
        (
            d
            for d in docs
            if seeker_effective_status(d, reviews_by_doc.get(d.id, []))
            == SeekerDocumentStatus.under_review
        ),
        None,
    )
    if reviewing is not None:
        return "under_review", reviewing.id
    rejected = next(
        (
            d
            for d in docs
            if seeker_effective_status(d, reviews_by_doc.get(d.id, []))
            == SeekerDocumentStatus.rejected
        ),
        None,
    )
    if rejected is not None:
        return "rejected", rejected.id
    expired = next(
        (
            d
            for d in docs
            if seeker_effective_status(d, reviews_by_doc.get(d.id, []))
            == SeekerDocumentStatus.expired
        ),
        None,
    )
    if expired is not None:
        return "expired", expired.id
    return "under_review", docs[0].id


async def portfolio_summary(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    settings: Settings,
    visa_type: VisaType | None = None,
) -> DocumentPortfolioSummary:
    """Overview tallies + required-category checklist for the Documents page.

    Default (no ``visa_type``) is portfolio-wide. Progress is share of required
    categories that have ≥1 active file (any status except missing).
    Also returns ``expiring_soon``: active docs whose ``expires_at`` falls within
    the next 30 days (inclusive of today), sorted soonest-first and capped at 20.
    """
    await refresh_expired_statuses(session, seeker_id)
    stmt = list_by_seeker_stmt(seeker_id, visa_type=visa_type)
    docs = list((await session.execute(stmt)).scalars().all())
    reviews_by_doc = await reviews_by_document(session, [d.id for d in docs])

    total = len(docs)
    approved = sum(
        1
        for d in docs
        if seeker_effective_status(d, reviews_by_doc.get(d.id, []))
        == SeekerDocumentStatus.approved
    )
    under_review = sum(
        1
        for d in docs
        if seeker_effective_status(d, reviews_by_doc.get(d.id, []))
        == SeekerDocumentStatus.under_review
    )
    rejected = sum(
        1
        for d in docs
        if seeker_effective_status(d, reviews_by_doc.get(d.id, []))
        == SeekerDocumentStatus.rejected
    )

    by_category: dict[DocumentCategory, list[SeekerDocument]] = defaultdict(list)
    for doc in docs:
        by_category[doc.category].append(doc)

    checklist: list[DocumentChecklistItem] = []
    missing = 0
    filled = 0
    for category in REQUIRED_CHECKLIST:
        status, document_id = _checklist_status_for_docs(
            by_category.get(category, []), reviews_by_doc
        )
        if status == "missing":
            missing += 1
        else:
            filled += 1
        checklist.append(
            DocumentChecklistItem(
                category=category,
                label=CHECKLIST_LABELS[category],
                status=status,
                document_id=document_id,
            )
        )

    required_n = len(REQUIRED_CHECKLIST)
    progress_percent = int(round(100 * filled / required_n)) if required_n else 0

    expiring_soon_docs = await _expiring_soon_docs(session, seeker_id, visa_type=visa_type)
    expiring_soon = await build_reads(
        session, expiring_soon_docs, settings, include_unread=True
    )

    return DocumentPortfolioSummary(
        total=total,
        approved=approved,
        under_review=under_review,
        missing=missing,
        rejected=rejected,
        progress_percent=progress_percent,
        checklist=checklist,
        expiring_soon=expiring_soon,
    )


EXPIRING_SOON_WINDOW_DAYS = 30
EXPIRING_SOON_MAX_ITEMS = 20


async def _expiring_soon_docs(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    visa_type: VisaType | None = None,
) -> list[SeekerDocument]:
    """Active docs expiring in the next ``EXPIRING_SOON_WINDOW_DAYS`` days.

    Future-only (``expires_at`` >= today), sorted soonest-first, capped at
    ``EXPIRING_SOON_MAX_ITEMS``. Same visa scope as the rest of the summary
    (untagged docs included when ``visa_type`` is set).
    """
    today = date.today()
    cutoff = today + timedelta(days=EXPIRING_SOON_WINDOW_DAYS)
    stmt = list_by_seeker_stmt(seeker_id, visa_type=visa_type).where(
        SeekerDocument.expires_at.is_not(None),
        SeekerDocument.expires_at >= today,
        SeekerDocument.expires_at <= cutoff,
    )
    stmt = stmt.order_by(SeekerDocument.expires_at.asc()).limit(EXPIRING_SOON_MAX_ITEMS)
    return list((await session.execute(stmt)).scalars().all())


async def add_comment(
    session: AsyncSession, document: SeekerDocument, author_id: uuid.UUID, body: str
) -> SeekerDocumentComment:
    comment = SeekerDocumentComment(
        document_id=document.id,
        author_id=author_id,
        body=body,
        created_by=author_id,
    )
    session.add(comment)
    await session.flush()
    await session.refresh(comment)
    return comment


async def mark_comments_read(
    session: AsyncSession, document: SeekerDocument, actor_id: uuid.UUID
) -> None:
    """Idempotent: seeker opened the comments sheet."""
    document.comments_last_read_at = datetime.now(UTC)
    document.updated_by = actor_id
    session.add(document)
    await session.flush()


async def notify_seeker_of_advisor_comment(
    session: AsyncSession,
    document: SeekerDocument,
    advisor: User,
    comment_body: str,
) -> None:
    """In-app + FCM outbox for an advisor comment. Caller sends email separately."""
    advisor_name = advisor.full_name or "Your advisor"
    preview = " ".join(comment_body.split())
    if len(preview) > 120:
        preview = preview[:117] + "..."
    body = f'{advisor_name} commented on "{document.document_name}"'
    if preview:
        body = f"{body}: {preview}"
    if len(body) > 1000:
        body = body[:997] + "..."
    await notification_service.notify(
        session,
        user_id=document.seeker_id,
        type=NotificationType.document_comment,
        title="New comment on your document",
        body=body,
        entity_type=NotificationEntityType.seeker_document,
        entity_id=document.id,
        actor_id=advisor.id,
    )


def list_comments_stmt(document_id: uuid.UUID) -> Select[tuple[SeekerDocumentComment]]:
    return (
        select(SeekerDocumentComment)
        .where(
            SeekerDocumentComment.document_id == document_id,
            SeekerDocumentComment.is_archived.is_(False),
        )
        .order_by(SeekerDocumentComment.created_at.asc())
    )


async def comment_counts_for_documents(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Active comment totals per document (excludes archived comments)."""
    if not document_ids:
        return {}
    rows = (
        await session.execute(
            select(SeekerDocumentComment.document_id, func.count())
            .where(
                SeekerDocumentComment.document_id.in_(document_ids),
                SeekerDocumentComment.is_archived.is_(False),
            )
            .group_by(SeekerDocumentComment.document_id)
        )
    ).all()
    return {doc_id: int(n) for doc_id, n in rows}


async def unread_comment_flags(
    session: AsyncSession, documents: list[SeekerDocument]
) -> dict[uuid.UUID, bool]:
    """True when an advisor/admin comment is newer than the seeker's last read."""
    if not documents:
        return {}
    ids = [d.id for d in documents]
    unread_ids = set(
        (
            await session.execute(
                select(SeekerDocumentComment.document_id)
                .join(SeekerDocument, SeekerDocument.id == SeekerDocumentComment.document_id)
                .where(
                    SeekerDocumentComment.document_id.in_(ids),
                    SeekerDocumentComment.is_archived.is_(False),
                    SeekerDocumentComment.author_id != SeekerDocument.seeker_id,
                    or_(
                        SeekerDocument.comments_last_read_at.is_(None),
                        SeekerDocumentComment.created_at > SeekerDocument.comments_last_read_at,
                    ),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return {doc_id: doc_id in unread_ids for doc_id in ids}


def build_read(
    document: SeekerDocument,
    settings: Settings,
    *,
    comments_count: int = 0,
    has_unread_comments: bool = False,
    status: SeekerDocumentStatus | None = None,
    reviewed_at: datetime | None = None,
    reviewed_by: uuid.UUID | None = None,
) -> SeekerDocumentRead:
    return SeekerDocumentRead(
        id=document.id,
        seeker_id=document.seeker_id,
        category=document.category,
        document_name=document.document_name,
        file_url=resolve_url(document.file_url, settings),
        file_size_bytes=document.file_size_bytes,
        content_type=document.content_type,
        status=status if status is not None else document.status,
        expires_at=document.expires_at,
        visa_type=parse_visa_type(document.visa_type),
        reviewed_at=reviewed_at if reviewed_at is not None else document.reviewed_at,
        reviewed_by=reviewed_by if reviewed_by is not None else document.reviewed_by,
        created_at=document.created_at,
        comments_count=comments_count,
        has_unread_comments=has_unread_comments,
    )


async def build_reads(
    session: AsyncSession,
    documents: list[SeekerDocument],
    settings: Settings,
    *,
    include_unread: bool = False,
    advisor_id: uuid.UUID | None = None,
) -> list[SeekerDocumentRead]:
    counts = await comment_counts_for_documents(session, [d.id for d in documents])
    unread = (
        await unread_comment_flags(session, documents)
        if include_unread
        else {d.id: False for d in documents}
    )
    reviews: dict[uuid.UUID, SeekerDocumentAdvisorReview] = {}
    seeker_reviews: dict[uuid.UUID, list[SeekerDocumentAdvisorReview]] = {}
    if documents:
        if advisor_id is not None:
            reviews = await reviews_for_advisor(session, [d.id for d in documents], advisor_id)
        else:
            seeker_reviews = await reviews_by_document(session, [d.id for d in documents])
    reads: list[SeekerDocumentRead] = []
    for d in documents:
        if advisor_id is not None:
            review = reviews.get(d.id)
            reads.append(
                build_read(
                    d,
                    settings,
                    comments_count=counts.get(d.id, 0),
                    has_unread_comments=unread.get(d.id, False),
                    status=advisor_effective_status(d, review),
                    reviewed_at=review.reviewed_at if review is not None else None,
                    reviewed_by=review.advisor_id if review is not None else None,
                )
            )
        else:
            doc_reviews = seeker_reviews.get(d.id, [])
            latest = _latest_review(doc_reviews)
            reads.append(
                build_read(
                    d,
                    settings,
                    comments_count=counts.get(d.id, 0),
                    has_unread_comments=unread.get(d.id, False),
                    status=seeker_effective_status(d, doc_reviews),
                    reviewed_at=latest.reviewed_at if latest is not None else None,
                    reviewed_by=latest.advisor_id if latest is not None else None,
                )
            )
    return reads


async def build_read_enriched(
    session: AsyncSession,
    document: SeekerDocument,
    settings: Settings,
    *,
    include_unread: bool = False,
    advisor_id: uuid.UUID | None = None,
) -> SeekerDocumentRead:
    counts = await comment_counts_for_documents(session, [document.id])
    unread = False
    if include_unread:
        flags = await unread_comment_flags(session, [document])
        unread = flags.get(document.id, False)
    review: SeekerDocumentAdvisorReview | None = None
    if advisor_id is not None:
        reviews = await reviews_for_advisor(session, [document.id], advisor_id)
        review = reviews.get(document.id)
        return build_read(
            document,
            settings,
            comments_count=counts.get(document.id, 0),
            has_unread_comments=unread,
            status=advisor_effective_status(document, review),
            reviewed_at=review.reviewed_at if review is not None else None,
            reviewed_by=review.advisor_id if review is not None else None,
        )
    doc_reviews = list(
        (await reviews_by_document(session, [document.id])).get(document.id, [])
    )
    latest = _latest_review(doc_reviews)
    return build_read(
        document,
        settings,
        comments_count=counts.get(document.id, 0),
        has_unread_comments=unread,
        status=seeker_effective_status(document, doc_reviews),
        reviewed_at=latest.reviewed_at if latest is not None else None,
        reviewed_by=latest.advisor_id if latest is not None else None,
    )


async def build_client_seeker_brief(
    session: AsyncSession, seeker_id: uuid.UUID, settings: Settings
) -> ClientSeekerBrief | None:
    """Seeker name/email/photo for the client-documents detail header."""
    seeker = await session.get(User, seeker_id)
    if seeker is None:
        return None
    photos = await booking_service.seeker_photo_keys(session, {seeker_id})
    return ClientSeekerBrief(
        seeker_id=seeker.id,
        seeker_name=seeker.full_name,
        seeker_email=seeker.email,
        seeker_profile_photo_url=resolve_media_url(photos.get(seeker.id), settings),
    )


def build_comment_read(comment: SeekerDocumentComment, author: User | None) -> DocumentCommentRead:
    author_role: DocumentCommentAuthorRole | None = None
    if author is not None:
        author_role = author.role.value
    return DocumentCommentRead(
        id=comment.id,
        document_id=comment.document_id,
        author_id=comment.author_id,
        author_name=author.full_name if author else None,
        author_role=author_role,
        body=comment.body,
        created_at=comment.created_at,
    )


def _row_documents_status(
    count: int, under_review: int, approved: int, rejected: int
) -> CustomerDocumentsRowStatus:
    """Map portfolio tallies to the FE Pending / Completed / Rejected badge."""
    if count == 0:
        return "pending"
    if under_review > 0:
        return "pending"
    if rejected == count:
        return "rejected"
    if approved == count:
        return "completed"
    return "pending"


def _portfolio_has_docs_clause() -> ColumnElement[bool]:
    return exists(
        select(SeekerDocument.id).where(
            SeekerDocument.seeker_id == Booking.seeker_id,
            SeekerDocument.is_archived.is_(False),
        )
    )


def _advisor_approved_review_exists() -> ColumnElement[bool]:
    return exists(
        select(SeekerDocumentAdvisorReview.id).where(
            SeekerDocumentAdvisorReview.document_id == SeekerDocument.id,
            SeekerDocumentAdvisorReview.advisor_id == Booking.advisor_id,
            SeekerDocumentAdvisorReview.is_archived.is_(False),
            SeekerDocumentAdvisorReview.status == SeekerDocumentStatus.approved,
        )
    )


def _advisor_rejected_review_exists() -> ColumnElement[bool]:
    return exists(
        select(SeekerDocumentAdvisorReview.id).where(
            SeekerDocumentAdvisorReview.document_id == SeekerDocument.id,
            SeekerDocumentAdvisorReview.advisor_id == Booking.advisor_id,
            SeekerDocumentAdvisorReview.is_archived.is_(False),
            SeekerDocumentAdvisorReview.status == SeekerDocumentStatus.rejected,
        )
    )


def _portfolio_completed_clause() -> ColumnElement[bool]:
    """Seeker has ≥1 doc and this booking's advisor approved every one."""
    has_open = exists(
        select(SeekerDocument.id)
        .where(
            SeekerDocument.seeker_id == Booking.seeker_id,
            SeekerDocument.is_archived.is_(False),
            or_(
                SeekerDocument.status == SeekerDocumentStatus.expired,
                ~_advisor_approved_review_exists(),
            ),
        )
        .correlate(Booking)
    )
    return _portfolio_has_docs_clause() & ~has_open


def _portfolio_rejected_clause() -> ColumnElement[bool]:
    """Seeker has ≥1 doc and this booking's advisor rejected every one."""
    has_non_rejected = exists(
        select(SeekerDocument.id)
        .where(
            SeekerDocument.seeker_id == Booking.seeker_id,
            SeekerDocument.is_archived.is_(False),
            or_(
                SeekerDocument.status == SeekerDocumentStatus.expired,
                ~_advisor_rejected_review_exists(),
            ),
        )
        .correlate(Booking)
    )
    return _portfolio_has_docs_clause() & ~has_non_rejected


def list_customer_documents_stmt(
    advisor_id: uuid.UUID,
    *,
    q: str | None = None,
    service_ids: list[uuid.UUID] | None = None,
    documents_status: CustomerDocumentsRowStatus | None = None,
    sort: BookingSort = "-scheduled_start",
) -> Select[tuple[Booking]]:
    """Advisor bookings that back the Documents-of-customers table (one row each)."""
    stmt = booking_service.list_for_user_stmt(
        advisor_id,
        UserRole.advisor,
        status=None,
        seeker_id=None,
        date_from=None,
        date_to=None,
        service_ids=service_ids,
        q=q,
        sort=sort,
    )
    if documents_status == "completed":
        stmt = stmt.where(_portfolio_completed_clause())
    elif documents_status == "rejected":
        stmt = stmt.where(_portfolio_rejected_clause())
    elif documents_status == "pending":
        stmt = stmt.where(~_portfolio_completed_clause() & ~_portfolio_rejected_clause())
    return stmt


async def build_customer_document_rows(
    session: AsyncSession,
    bookings: list[Booking],
    settings: Settings,
) -> list[CustomerDocumentsRowRead]:
    """Enrich bookings with seeker identity + portfolio document tallies."""
    if not bookings:
        return []

    seeker_ids = list({b.seeker_id for b in bookings})
    seekers = {
        u.id: u
        for u in (await session.execute(select(User).where(User.id.in_(seeker_ids))))
        .scalars()
        .all()
    }
    photos = await booking_service.seeker_photo_keys(session, set(seeker_ids))

    docs = list(
        (
            await session.execute(
                select(SeekerDocument).where(
                    SeekerDocument.seeker_id.in_(seeker_ids),
                    SeekerDocument.is_archived.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    docs_by_seeker: dict[uuid.UUID, list[SeekerDocument]] = defaultdict(list)
    for doc in docs:
        docs_by_seeker[doc.seeker_id].append(doc)

    advisor_ids = {b.advisor_id for b in bookings}
    reviews_by_doc_advisor: dict[tuple[uuid.UUID, uuid.UUID], SeekerDocumentAdvisorReview] = {}
    if docs:
        reviews = (
            await session.execute(
                select(SeekerDocumentAdvisorReview).where(
                    SeekerDocumentAdvisorReview.document_id.in_([d.id for d in docs]),
                    SeekerDocumentAdvisorReview.advisor_id.in_(advisor_ids),
                    SeekerDocumentAdvisorReview.is_archived.is_(False),
                )
            )
        ).scalars()
        reviews_by_doc_advisor = {
            (review.document_id, review.advisor_id): review for review in reviews
        }

    counts: dict[tuple[uuid.UUID, uuid.UUID], dict[str, int]] = {}
    latest_doc_at: dict[uuid.UUID, datetime] = {}
    for seeker_id, advisor_id in {(b.seeker_id, b.advisor_id) for b in bookings}:
        pair = (seeker_id, advisor_id)
        bucket = {"total": 0, "under_review": 0, "approved": 0, "rejected": 0}
        for doc in docs_by_seeker.get(seeker_id, []):
            bucket["total"] += 1
            effective = advisor_effective_status(
                doc, reviews_by_doc_advisor.get((doc.id, advisor_id))
            )
            if effective == SeekerDocumentStatus.under_review:
                bucket["under_review"] += 1
            elif effective == SeekerDocumentStatus.approved:
                bucket["approved"] += 1
            elif effective == SeekerDocumentStatus.rejected:
                bucket["rejected"] += 1
            if doc.updated_at is not None:
                prev = latest_doc_at.get(seeker_id)
                if prev is None or doc.updated_at > prev:
                    latest_doc_at[seeker_id] = doc.updated_at
        counts[pair] = bucket

    rows: list[CustomerDocumentsRowRead] = []
    notice_map = await booking_service.notice_hours_by_advisor(
        session, {b.advisor_id for b in bookings}
    )
    for booking in bookings:
        seeker = seekers.get(booking.seeker_id)
        if seeker is None:
            continue
        tallies = counts.get(
            (booking.seeker_id, booking.advisor_id),
            {"total": 0, "under_review": 0, "approved": 0, "rejected": 0},
        )
        status = _row_documents_status(
            tallies["total"],
            tallies["under_review"],
            tallies["approved"],
            tallies["rejected"],
        )
        updated = latest_doc_at.get(booking.seeker_id) or booking.updated_at or booking.created_at
        notice_hours = notice_map.get(booking.advisor_id, booking_service.DEFAULT_NOTICE_HOURS)
        can_reschedule, _can_cancel = booking_service.compute_capabilities(
            booking,
            cancellation_notice_hours=notice_hours,
            viewer_role=UserRole.advisor,
        )
        rows.append(
            CustomerDocumentsRowRead(
                booking_id=booking.id,
                appointment_id=booking_service.appointment_id_str(booking),
                seeker_id=seeker.id,
                seeker_name=seeker.full_name,
                seeker_email=seeker.email,
                seeker_profile_photo_url=resolve_media_url(photos.get(seeker.id), settings),
                service_id=booking.service_id,
                name=booking.name,
                booking_status=booking.status,
                documents_count=tallies["total"],
                documents_status=status,
                scheduled_start=as_utc(booking.scheduled_start),
                scheduled_end=as_utc(booking.scheduled_end),
                can_reschedule=can_reschedule,
                updated_at=updated,
            )
        )
    return rows


async def notify_seeker_of_document_status_update(
    session: AsyncSession,
    document: SeekerDocument,
    advisor: User,
    status: str,
    note: str | None = None,
) -> None:
    advisor_name = advisor.full_name or "Your advisor"
    status_capitalized = status.capitalize()
    
    body = f'{advisor_name} {status} your document "{document.document_name}"'
    if note:
        preview = " ".join(note.split())
        if len(preview) > 120:
            preview = preview[:117] + "..."
        body = f"{body}: {preview}"
    if len(body) > 1000:
        body = body[:997] + "..."
        
    await notification_service.notify(
        session,
        user_id=document.seeker_id,
        type=NotificationType.document_status_updated,
        title=f"Document {status_capitalized}",
        body=body,
        entity_type=NotificationEntityType.seeker_document,
        entity_id=document.id,
        actor_id=advisor.id,
    )

