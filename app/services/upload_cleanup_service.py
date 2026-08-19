"""TTL cleanup for upload objects that were never attached to a domain row."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.file_storage import stored_url_to_key, sweep_unreferenced_uploads
from app.models.seeker_document import SeekerDocument


async def sweep_orphan_seeker_uploads(session: AsyncSession, settings: Settings) -> int:
    """Delete ``seeker_document/`` files older than the TTL with no document row.

    Archived documents still reference their file — those keys are kept.
    """
    urls = (await session.execute(select(SeekerDocument.file_url))).scalars().all()
    referenced = {key for url in urls if (key := stored_url_to_key(url)) is not None}
    return sweep_unreferenced_uploads(
        settings,
        prefix="seeker_document/",
        referenced_keys=referenced,
        older_than=timedelta(hours=settings.UPLOAD_ORPHAN_TTL_HOURS),
    )
