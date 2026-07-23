"""Seeker document portfolio — upload, listing, review, and comment thread."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.file_storage import resolve_media_url, resolve_url
from app.models.booking import Booking
from app.models.seeker_document import (
    SeekerDocument,
    SeekerDocumentComment,
    SeekerDocumentStatus,
)
from app.models.user import User, UserRole
from app.schemas.booking import BookingSort
from app.schemas.seeker_document import (
    CustomerDocumentsRowRead,
    CustomerDocumentsRowStatus,
    DocumentCommentRead,
    SeekerDocumentCreate,
    SeekerDocumentRead,
    SeekerDocumentStatusUpdate,
)
from app.services import booking_service


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
        created_by=seeker_id,
    )
    session.add(document)
    await session.flush()
    await session.refresh(document)
    return document


def list_by_seeker_stmt(seeker_id: uuid.UUID) -> Select[tuple[SeekerDocument]]:
    return (
        select(SeekerDocument)
        .where(SeekerDocument.seeker_id == seeker_id)
        .where(SeekerDocument.is_archived.is_(False))
        .order_by(SeekerDocument.created_at.desc())
    )


async def get_for_seeker(
    session: AsyncSession, document_id: uuid.UUID, seeker_id: uuid.UUID
) -> SeekerDocument:
    document = await session.get(SeekerDocument, document_id)
    if document is None or document.seeker_id != seeker_id:
        raise NotFoundError("Document not found")
    return document


async def get_by_id(session: AsyncSession, document_id: uuid.UUID) -> SeekerDocument:
    document = await session.get(SeekerDocument, document_id)
    if document is None:
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
        reviewed_at=document.reviewed_at,
        reviewed_by=document.reviewed_by,
        created_at=document.created_at,
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
    """Map portfolio tallies to the FE Pending / Completed badge."""
    if count == 0:
        return "pending"
    if under_review > 0 or rejected > 0:
        return "pending"
    if approved == count:
        return "completed"
    return "pending"


def _portfolio_completed_clause() -> ColumnElement[bool]:
    """Seeker has ≥1 doc and none under_review/rejected (all approved)."""
    has_docs = exists(
        select(SeekerDocument.id).where(
            SeekerDocument.seeker_id == Booking.seeker_id,
            SeekerDocument.is_archived.is_(False),
        )
    )
    has_open = exists(
        select(SeekerDocument.id).where(
            SeekerDocument.seeker_id == Booking.seeker_id,
            SeekerDocument.is_archived.is_(False),
            SeekerDocument.status.in_(
                (SeekerDocumentStatus.under_review, SeekerDocumentStatus.rejected)
            ),
        )
    )
    return has_docs & ~has_open


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
    elif documents_status == "pending":
        stmt = stmt.where(~_portfolio_completed_clause())
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
