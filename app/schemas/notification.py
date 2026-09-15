"""Schemas for the in-app notification feed."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.notification import NotificationEntityType, NotificationType


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: NotificationType
    title: str
    body: str
    entity_type: NotificationEntityType | None
    entity_id: uuid.UUID | None
    actor_id: uuid.UUID | None
    read_at: datetime | None
    created_at: datetime
    # Raw instant for booking-related notifications, so the client can render it
    # in the viewer's own timezone. Null for notification types with no booking.
    scheduled_start: datetime | None


class UnreadCountRead(BaseModel):
    unread: int


class ReadAllResult(BaseModel):
    updated: int
