"""Seeker document portfolio — upload, listing, review, and comment thread."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import ColumnElement, Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.file_storage import resolve_media_url, resolve_url
from app.core.visa_types import parse_visa_type
from app.models.booking import Booking
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
    DocumentCommentRead,
    DocumentPortfolioSummary,
    SeekerDocumentCreate,
    SeekerDocumentRead,
    SeekerDocumentStatusUpdate,
    SeekerDocumentUpdate,
)
from app.services import booking_service

# Required portfolio categories for the seeker Documents checklist / Missing card.
REQUIRED_CHECKLIST: tuple[tuple[DocumentCategory, str], ...] = (
    (DocumentCategory.passport, "Passport"),
    (DocumentCategory.finance, "Bank Statement"),
    (DocumentCategory.supporting, "Employment Letter"),
    (DocumentCategory.educational, "Academic Certification"),
)


async def create(
    session: AsyncSession, seeker_id: uuid.UUID, data: SeekerDocumentCreate, file_url: str
) -> SeekerDocument:
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
) -> SeekerDocument:
    if data.document_name is not None:
        document.document_name = data.document_name
    if data.clear_expires_at:
        document.expires_at = None
    elif data.expires_at is not None:
        document.expires_at = data.expires_at
    if data.clear_visa_type:
        document.visa_type = None
    elif data.visa_type is not None:
        document.visa_type = data.visa_type.value
    document.updated_by = actor_id
    session.add(document)
    await session.flush()
    await session.refresh(document)
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
        cutoff = date.today() + timedelta(days=expiring_within_days)
        stmt = stmt.where(SeekerDocument.expires_at.is_not(None)).where(
            SeekerDocument.expires_at <= cutoff
        )
    if expires_before is not None:
        stmt = stmt.where(SeekerDocument.expires_at.is_not(None)).where(
            SeekerDocument.expires_at <= expires_before
        )
    return stmt.order_by(SeekerDocument.created_at.desc())


async def get_for_seeker(
    session: AsyncSession, document_id: uuid.UUID, seeker_id: uuid.UUID
) -> SeekerDocument:
    document = await session.get(SeekerDocument, document_id)
    if document is None or document.seeker_id != seeker_id or document.is_archived:
        raise NotFoundError("Document not found")
    return document


async def get_by_id(session: AsyncSession, document_id: uuid.UUID) -> SeekerDocument:
    document = await session.get(SeekerDocument, document_id)
    if document is None or document.is_archived:
        raise NotFoundError("Document not found")
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
    rejected = docs[0]
    return "rejected", rejected.id


async def portfolio_summary(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    visa_type: VisaType | None = None,
) -> DocumentPortfolioSummary:
    """Overview tallies + required-category checklist for the Documents page."""
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
    required_approved = 0
    for category, label in REQUIRED_CHECKLIST:
        status, document_id = _checklist_status_for_docs(by_category.get(category, []))
        if status == "missing":
            missing += 1
        elif status == "approved":
            required_approved += 1
        checklist.append(
            DocumentChecklistItem(
                category=category,
                label=label,
                status=status,
                document_id=document_id,
            )
        )

    required_n = len(REQUIRED_CHECKLIST)
    progress_percent = int(round(100 * required_approved / required_n)) if required_n else 0

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


def list_comments_stmt(document_id: uuid.UUID) -> Select[tuple[SeekerDocumentComment]]:
    return (
        select(SeekerDocumentComment)
        .where(SeekerDocumentComment.document_id == document_id)
        .order_by(SeekerDocumentComment.created_at.asc())
    )


def build_read(document: SeekerDocument, settings: Settings) -> SeekerDocumentRead:
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
    return DocumentCommentRead(
        id=comment.id,
        document_id=comment.document_id,
        author_id=comment.author_id,
        author_name=author.full_name if author else None,
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
                (SeekerDocumentStatus.under_review, SeekerDocumentStatus.rejected)
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
                updated_at=updated,
            )
        )
    return rows
