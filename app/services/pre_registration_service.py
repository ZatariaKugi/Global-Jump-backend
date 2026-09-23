"""Pre-registration leads: public submit, admin listing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.rate_limit import enforce_cooldown
from app.models.notification import NotificationType
from app.models.pre_registration import PreRegistration, PreRegistrationInterest
from app.schemas.pre_registration import PreRegistrationCreate
from app.services import notification_service

RESUBMIT_COOLDOWN_SECONDS = 60


async def create(session: AsyncSession, data: PreRegistrationCreate) -> PreRegistration:
    email = data.email.lower()
    enforce_cooldown(f"pre_registration:{email}", cooldown_seconds=RESUBMIT_COOLDOWN_SECONDS)

    row = PreRegistration(
        full_name=data.full_name,
        email=email,
        phone=data.phone,
        country=data.country,
        city=data.city,
        interest=data.interest,
        message=data.message,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)

    await notification_service.notify_admins(
        session,
        type=NotificationType.pre_registration_received,
        title="New pre-registration",
        body=f"{row.full_name} ({row.interest.value}) from {row.city}, {row.country}",
        entity_id=row.id,
    )
    return row


def list_stmt(
    search: str | None = None,
    interest: PreRegistrationInterest | None = None,
) -> Select[tuple[PreRegistration]]:
    stmt = select(PreRegistration).where(PreRegistration.is_archived.is_(False))
    if interest is not None:
        stmt = stmt.where(PreRegistration.interest == interest)
    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                PreRegistration.full_name.ilike(pattern),
                PreRegistration.email.ilike(pattern),
                PreRegistration.city.ilike(pattern),
            )
        )
    return stmt.order_by(PreRegistration.created_at.desc(), PreRegistration.id.desc())


async def get_by_id(session: AsyncSession, row_id: uuid.UUID) -> PreRegistration:
    row = await session.get(PreRegistration, row_id)
    if row is None or row.is_archived:
        raise NotFoundError("Pre-registration not found")
    return row
