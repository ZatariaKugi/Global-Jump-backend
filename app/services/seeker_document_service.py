"""Seeker document portfolio — upload, listing, review, and comment thread."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import ColumnElement, Select, exists, func, or_, select
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
    if document is None or document.seeker_id != seeker_id or document.is_archived:
        raise NotFoundError("Document not found")
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
    document.status = status.status
    document.reviewed_at = datetime.now(UTC)
    document.reviewed_by = reviewer_id
    document.updated_by = reviewer_id
    session.add(document)
    await session.flush()
    await session.refresh(document)
    return document


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
) -> tuple[ChecklistItemStatus, uuid.UUID | None]:
    """Pick the best status for a required category from matching uploads."""
    if not docs:
        return "missing", None
    approved = next((d for d in docs if d.status == SeekerDocumentStatus.approved), None)
    if approved is not None:
        return "approved", approved.id
    reviewing = next((d for d in docs if d.status == SeekerDocumentStatus.under_review), None)
    if reviewing is not None:
        return "under_review", reviewing.id
    rejected = next((d for d in docs if d.status == SeekerDocumentStatus.rejected), None)
    if rejected is not None:
        return "rejected", rejected.id
    expired = next((d for d in docs if d.status == SeekerDocumentStatus.expired), None)
    if expired is not None:
        return "expired", expired.id
    return "under_review", docs[0].id


async def portfolio_summary(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    visa_type: VisaType | None = None,
) -> DocumentPortfolioSummary:
    """Overview tallies + required-category checklist for the Documents page.

    Default (no ``visa_type``) is portfolio-wide. Progress is share of required
    categories that have ≥1 active file (any status except missing).
    """
    await refresh_expired_statuses(session, seeker_id)
    stmt = list_by_seeker_stmt(seeker_id, visa_type=visa_type)
    docs = list((await session.execute(stmt)).scalars().all())

    total = len(docs)
    approved = sum(1 for d in docs if d.status == SeekerDocumentStatus.approved)
    under_review = sum(1 for d in docs if d.status == SeekerDocumentStatus.under_review)
    rejected = sum(1 for d in docs if d.status == SeekerDocumentStatus.rejected)

    by_category: dict[DocumentCategory, list[SeekerDocument]] = defaultdict(list)
    for doc in docs:
        by_category[doc.category].append(doc)

    checklist: list[DocumentChecklistItem] = []
    missing = 0
    filled = 0
    for category in REQUIRED_CHECKLIST:
        status, document_id = _checklist_status_for_docs(by_category.get(category, []))
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

    return DocumentPortfolioSummary(
        total=total,
        approved=approved,
        under_review=under_review,
        missing=missing,
        rejected=rejected,
        progress_percent=progress_percent,
        checklist=checklist,
    )


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
) -> SeekerDocumentRead:
    return SeekerDocumentRead(
        id=document.id,
        seeker_id=document.seeker_id,
        category=document.category,
        document_name=document.document_name,
        file_url=resolve_url(document.file_url, settings),
        file_size_bytes=document.file_size_bytes,
        content_type=document.content_type,
        status=document.status,
        expires_at=document.expires_at,
        visa_type=parse_visa_type(document.visa_type),
        reviewed_at=document.reviewed_at,
        reviewed_by=document.reviewed_by,
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
) -> list[SeekerDocumentRead]:
    counts = await comment_counts_for_documents(session, [d.id for d in documents])
    unread = (
        await unread_comment_flags(session, documents)
        if include_unread
        else {d.id: False for d in documents}
    )
    return [
        build_read(
            d,
            settings,
            comments_count=counts.get(d.id, 0),
            has_unread_comments=unread.get(d.id, False),
        )
        for d in documents
    ]


async def build_read_enriched(
    session: AsyncSession,
    document: SeekerDocument,
    settings: Settings,
    *,
    include_unread: bool = False,
) -> SeekerDocumentRead:
    counts = await comment_counts_for_documents(session, [document.id])
    unread = False
    if include_unread:
        flags = await unread_comment_flags(session, [document])
        unread = flags.get(document.id, False)
    return build_read(
        document,
        settings,
        comments_count=counts.get(document.id, 0),
        has_unread_comments=unread,
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


def _portfolio_completed_clause() -> ColumnElement[bool]:
    """Seeker has ≥1 doc and none under_review/rejected (all approved)."""
    has_open = exists(
        select(SeekerDocument.id).where(
            SeekerDocument.seeker_id == Booking.seeker_id,
            SeekerDocument.is_archived.is_(False),
            SeekerDocument.status.in_(
                (
                    SeekerDocumentStatus.under_review,
                    SeekerDocumentStatus.rejected,
                    SeekerDocumentStatus.expired,
                )
            ),
        )
    )
    return _portfolio_has_docs_clause() & ~has_open


def _portfolio_rejected_clause() -> ColumnElement[bool]:
    """Seeker has ≥1 doc, none under_review, and every doc is rejected."""
    has_under_review = exists(
        select(SeekerDocument.id).where(
            SeekerDocument.seeker_id == Booking.seeker_id,
            SeekerDocument.is_archived.is_(False),
            SeekerDocument.status == SeekerDocumentStatus.under_review,
        )
    )
    has_non_rejected = exists(
        select(SeekerDocument.id).where(
            SeekerDocument.seeker_id == Booking.seeker_id,
            SeekerDocument.is_archived.is_(False),
            SeekerDocument.status != SeekerDocumentStatus.rejected,
        )
    )
    return _portfolio_has_docs_clause() & ~has_under_review & ~has_non_rejected


def list_customer_documents_stmt(
    advisor_id: uuid.UUID,
    *,
    q: str | None = None,
    service_types: list[str] | None = None,
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
        service_types=service_types,
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

    doc_rows = (
        await session.execute(
            select(
                SeekerDocument.seeker_id,
                SeekerDocument.status,
                func.count(),
                func.max(SeekerDocument.updated_at),
            )
            .where(
                SeekerDocument.seeker_id.in_(seeker_ids),
                SeekerDocument.is_archived.is_(False),
            )
            .group_by(SeekerDocument.seeker_id, SeekerDocument.status)
        )
    ).all()

    counts: dict[uuid.UUID, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "under_review": 0, "approved": 0, "rejected": 0}
    )
    latest_doc_at: dict[uuid.UUID, datetime] = {}
    for seeker_id, status, n, max_updated in doc_rows:
        bucket = counts[seeker_id]
        bucket["total"] += int(n)
        if status == SeekerDocumentStatus.under_review:
            bucket["under_review"] += int(n)
        elif status == SeekerDocumentStatus.approved:
            bucket["approved"] += int(n)
        elif status == SeekerDocumentStatus.rejected:
            bucket["rejected"] += int(n)
        if max_updated is not None:
            prev = latest_doc_at.get(seeker_id)
            if prev is None or max_updated > prev:
                latest_doc_at[seeker_id] = max_updated

    rows: list[CustomerDocumentsRowRead] = []
    notice_map = await booking_service.notice_hours_by_advisor(
        session, {b.advisor_id for b in bookings}
    )
    for booking in bookings:
        seeker = seekers.get(booking.seeker_id)
        if seeker is None:
            continue
        tallies = counts[booking.seeker_id]
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
                service_type=booking.service_type,
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
