"""Advisor Zoom OAuth connection — one linked Zoom account per advisor."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class ZoomConnectionStatus(StrEnum):
    connected = "connected"
    disconnected = "disconnected"
    revoked = "revoked"
    error = "error"


class ZoomConnection(BaseModel):
    """Stores encrypted Zoom OAuth tokens for an advisor.

    Access and refresh tokens are AES-GCM encrypted (never returned to clients).
    ``advisor_id`` is the local ``users.id`` of an advisor account.
    """

    __tablename__ = "zoom_connections"
    __table_args__ = (UniqueConstraint("advisor_id", name="uq_zoom_connections_advisor_id"),)

    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    zoom_user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    zoom_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    zoom_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scopes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[ZoomConnectionStatus] = mapped_column(
        SAEnum(
            ZoomConnectionStatus,
            name="zoom_connection_status",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=ZoomConnectionStatus.connected,
        nullable=False,
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
