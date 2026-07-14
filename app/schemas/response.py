"""Standard API response envelope.

Every successful JSON response is wrapped as::

    {
      "success": true,
      "data": <payload>,
      "meta": {
        "request_id": "<uuid>",
        "timestamp": "2026-...",
        "pagination": {"total": 80, "page": 2, "page_size": 20, "pages": 4}  # list only
      }
    }

This mirrors the error envelope produced in ``app.core.exceptions`` so clients see a
single, predictable shape for both success and failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PageMeta(BaseModel):
    """Pagination metadata for list responses."""

    total: int
    page: int
    page_size: int
    pages: int


class Meta(BaseModel):
    request_id: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)
    pagination: PageMeta | None = None


class ResponseEnvelope[T](BaseModel):
    success: bool = True
    data: T
    meta: Meta = Field(default_factory=Meta)
