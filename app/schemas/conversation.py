"""Schemas for in-platform messaging (PRD §3.7)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.review import ModerationStatus


class ConversationCreate(BaseModel):
    other_user_id: uuid.UUID


class MessageAttachmentRef(BaseModel):
    """Reference to a file already uploaded via ``POST /uploads``
    (``category=message_attachment``).  Include one entry per file to attach."""

    file_key: str = Field(min_length=1, max_length=500)
    file_name: str = Field(min_length=1, max_length=255)
    file_size_bytes: int = Field(ge=1)
    content_type: str = Field(default="application/octet-stream", max_length=100)


class MessageSend(BaseModel):
    body: str | None = Field(default=None, max_length=5000)
    attachments: list[MessageAttachmentRef] = Field(default_factory=list)


class MessageEdit(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class AttachmentRead(BaseModel):
    id: uuid.UUID
    file_url: str
    file_name: str
    file_size: int
    content_type: str


class MessageRead(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sender_id: uuid.UUID
    sender_name: str | None
    body: str | None
    attachments: list[AttachmentRead]
    read_at: datetime | None
    edited_at: datetime | None
    created_at: datetime


class ConversationRead(BaseModel):
    id: uuid.UUID
    seeker_id: uuid.UUID
    advisor_id: uuid.UUID
    other_party_id: uuid.UUID
    other_party_name: str | None
    other_party_online: bool
    last_message_at: datetime | None
    last_message_preview: str | None
    unread_count: int
    created_at: datetime


class MessageReport(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class FlaggedMessageRead(MessageRead):
    moderation_status: ModerationStatus
    flag_reason: str | None
    flagged_by: uuid.UUID | None
