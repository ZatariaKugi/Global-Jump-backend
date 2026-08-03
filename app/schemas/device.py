"""Schemas for FCM device-token registration."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.device_token import DevicePlatform


class DeviceRegister(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    platform: DevicePlatform


class DeviceUnregister(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    token: str
    platform: DevicePlatform
    last_seen_at: datetime
    created_at: datetime
