"""Schemas for seeker advisor bookmarks (Bookmarked list screen)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BookmarkCreate(BaseModel):
    advisor_id: uuid.UUID


class BookmarkRead(BaseModel):
    """One bookmarked advisor row for the Bookmarked table."""

    id: uuid.UUID  # bookmark row id
    advisor_id: uuid.UUID
    full_name: str | None
    email: str
    profile_photo_url: str | None
    expertise: str | None  # title / specialty label
    average_rating: float | None
    years_of_experience: int | None
    offered_services: list[str] = Field(default_factory=list)
    starting_price_usd: float | None = None
    match_percentage: int | None = None  # 0–100, null when seeker has no destination/visa context
    status: Literal["active", "inactive"]
    public_profile_slug: str | None
    is_bookmarked: bool = True
    # Existing chat thread with this advisor; null if none yet
    conversation_id: uuid.UUID | None = None
    bookmarked_at: datetime
