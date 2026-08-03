"""FCM device registration tokens, one row per device."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class DevicePlatform(StrEnum):
    ios = "ios"
    android = "android"
    web = "web"


class DeviceToken(BaseModel):
    """An FCM registration token owned by a user.

    ``token`` is globally unique: re-registering an existing token reassigns it to
    the authenticating user (the device changed owner). Rows are deleted on logout
    and pruned when FCM reports the token unregistered.
    """

    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    platform: Mapped[DevicePlatform] = mapped_column(
        SAEnum(DevicePlatform, name="device_platform"), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
