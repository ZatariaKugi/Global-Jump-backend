"""Schemas for Advisor Zoom OAuth connection."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ZoomConnectRead(BaseModel):
    """Authorization URL the frontend should navigate to."""

    authorize_url: str = Field(
        description="Zoom OAuth authorize URL — FE should window.location to this"
    )


class ZoomStatusRead(BaseModel):
    connected: bool
    zoom_email: str | None = None
    connected_at: datetime | None = None
    status: str | None = None
