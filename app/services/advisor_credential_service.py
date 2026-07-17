"""Advisor credential document data-access and business logic."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.advisor_credential import AdvisorCredential, CredentialStatus
from app.schemas.advisor_admin import BulkCredentialReview
from app.schemas.advisor_credential import AdvisorCredentialCreate, CredentialStatusUpdate


async def create(
    session: AsyncSession,
    user_id: uuid.UUID,
    data: AdvisorCredentialCreate,
    file_url: str,
    file_size_bytes: int | None,
) -> AdvisorCredential:
    credential = AdvisorCredential(
        user_id=user_id,
        document_type=data.document_type,
        document_name=data.document_name,
        file_url=file_url,
        file_size_bytes=file_size_bytes,
        expiry_date=data.expiry_date,
        status=CredentialStatus.pending,
        created_by=user_id,
        updated_by=user_id,
    )
    session.add(credential)
    await session.flush()
    await session.refresh(credential)
    return credential


async def list_by_user(session: AsyncSession, user_id: uuid.UUID) -> list[AdvisorCredential]:
    result = await session.execute(
        select(AdvisorCredential)
        .where(AdvisorCredential.user_id == user_id)
        .where(AdvisorCredential.is_archived.is_(False))
        .order_by(AdvisorCredential.created_at.desc())
    )
    return list(result.scalars().all())


async def get_by_id(session: AsyncSession, credential_id: uuid.UUID) -> AdvisorCredential | None:
    return await session.get(AdvisorCredential, credential_id)


async def delete(session: AsyncSession, credential: AdvisorCredential) -> None:
    if credential.status != CredentialStatus.pending:
        from app.core.exceptions import AppError

        raise AppError("Only pending credentials can be deleted", code="credential_not_deletable")
    credential.archive(credential.user_id)
    session.add(credential)
    await session.flush()


async def update_status(
    session: AsyncSession,
    credential: AdvisorCredential,
    data: CredentialStatusUpdate,
    admin_id: uuid.UUID,
) -> AdvisorCredential:
    credential.status = data.status
    credential.admin_note = data.admin_note
    credential.updated_by = admin_id
    if data.status == CredentialStatus.verified:
        credential.verified_at = datetime.now(UTC)
        credential.verified_by = admin_id
    elif data.status == CredentialStatus.pending:
        credential.verified_at = None
        credential.verified_by = None
    session.add(credential)
    await session.flush()
    await session.refresh(credential)
    return credential


async def get_for_advisor_admin(
    session: AsyncSession, advisor_id: uuid.UUID, status: CredentialStatus | None = None
) -> list[AdvisorCredential]:
    stmt = (
        select(AdvisorCredential)
        .where(AdvisorCredential.user_id == advisor_id)
        .order_by(AdvisorCredential.created_at.desc())
    )
    if status is not None:
        stmt = stmt.where(AdvisorCredential.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def bulk_update_status(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    body: BulkCredentialReview,
    admin_id: uuid.UUID,
) -> list[AdvisorCredential]:
    """Approve or reject every currently-pending credential for one advisor.

    Loops over the existing single-record update_status() rather than a raw
    bulk UPDATE, to keep its verified_at/verified_by stamping logic in one
    place — per-advisor pending volume is a handful of documents, not worth
    the duplication risk of a second code path.
    """
    updated = await resolve_pending(
        session,
        advisor_id,
        CredentialStatus.verified if body.action == "approve" else CredentialStatus.rejected,
        admin_id,
        admin_note=body.admin_note,
    )
    if not updated:
        raise NotFoundError("No pending credentials for this advisor")
    return updated


async def resolve_pending(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    target: CredentialStatus,
    admin_id: uuid.UUID,
    *,
    admin_note: str | None = None,
) -> list[AdvisorCredential]:
    """Mark every pending credential for ``advisor_id`` as ``target``.

    Returns an empty list when there is nothing pending (unlike
    :func:`bulk_update_status`, which raises). Used by account-level
    approve/reject so the verification queue empties in the same request.
    """
    pending = await get_for_advisor_admin(session, advisor_id, status=CredentialStatus.pending)
    if not pending:
        return []
    update_body = CredentialStatusUpdate(status=target, admin_note=admin_note)
    return [await update_status(session, c, update_body, admin_id) for c in pending]


async def reopen_for_review(
    session: AsyncSession,
    advisor_id: uuid.UUID,
    admin_id: uuid.UUID,
    *,
    admin_note: str | None = None,
) -> list[AdvisorCredential]:
    """Set every non-pending credential back to pending (queue re-entry).

    Used when an admin moves the account to ``pending`` / ``under_review``
    after a prior approve/reject.
    """
    credentials = await get_for_advisor_admin(session, advisor_id)
    to_reopen = [c for c in credentials if c.status != CredentialStatus.pending]
    if not to_reopen:
        return []
    update_body = CredentialStatusUpdate(status=CredentialStatus.pending, admin_note=admin_note)
    return [await update_status(session, c, update_body, admin_id) for c in to_reopen]
