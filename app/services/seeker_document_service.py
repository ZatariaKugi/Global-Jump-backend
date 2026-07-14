"""Seeker document portfolio — upload, listing, review, and comment thread."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.file_storage import resolve_url
from app.models.seeker_document import SeekerDocument, SeekerDocumentComment
from app.models.user import User
from app.schemas.seeker_document import (
    DocumentCommentRead,
    SeekerDocumentCreate,
    SeekerDocumentRead,
    SeekerDocumentStatusUpdate,
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
