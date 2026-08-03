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


class UnreadCountRead(BaseModel):
    unread: int


class ReadAllResult(BaseModel):
    updated: int
