"""Records login activity for the admin analytics retention-curve widget."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity_log import ActivityLog


async def record_login(session: AsyncSession, user_id: uuid.UUID) -> None:
    """One row per user per UTC calendar day. Read-then-write; the small race
    window under concurrent logins in the same second is an accepted tradeoff
    for a dedup-only analytics signal."""
    today = datetime.now(UTC).date()
    exists = await session.execute(
        select(ActivityLog.id).where(
            ActivityLog.user_id == user_id, ActivityLog.occurred_on == today
        )
    )
    if exists.scalar_one_or_none() is not None:
        return
    session.add(ActivityLog(user_id=user_id, occurred_on=today))
    await session.flush()
