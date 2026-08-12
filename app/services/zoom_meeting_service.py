"""Zoom scheduled meeting API — create, update, delete on advisor OAuth tokens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.services.zoom_oauth_service import ZOOM_API_BASE

logger = get_logger(__name__)


@dataclass(slots=True)
class ZoomMeetingInfo:
    meeting_id: str
    join_url: str
    start_url: str
    passcode: str | None


def _format_zoom_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def create_scheduled_meeting(
    access_token: str,
    *,
    topic: str,
    start_utc: datetime,
    duration_minutes: int,
) -> ZoomMeetingInfo:
    """Create a scheduled Zoom meeting on the connected advisor account."""
    payload = {
        "topic": topic,
        "type": 2,
        "start_time": _format_zoom_time(start_utc),
        "duration": duration_minutes,
        "timezone": "UTC",
        "settings": {
            "join_before_host": False,
            "waiting_room": True,
            "approval_type": 2,
        },
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ZOOM_API_BASE}/users/me/meetings",
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("zoom_meeting_create_http_error", error=str(exc))
        raise AppError(
            "Unable to create the Zoom meeting. Please try again.",
            code="zoom_meeting_create_failed",
        ) from exc

    if response.status_code >= 400:
        logger.warning(
            "zoom_meeting_create_rejected",
            status_code=response.status_code,
            body=response.text[:500],
        )
        raise AppError(
            "Unable to create the Zoom meeting. The advisor may need to reconnect Zoom.",
            code="zoom_meeting_create_failed",
        )

    body = response.json()
    meeting_id = body.get("id")
    join_url = body.get("join_url")
    start_url = body.get("start_url")
    if not isinstance(meeting_id, int | str) or not join_url or not start_url:
        raise AppError(
            "Zoom returned an incomplete meeting response.",
            code="zoom_meeting_create_failed",
        )

    password = body.get("password")
    return ZoomMeetingInfo(
        meeting_id=str(meeting_id),
        join_url=str(join_url),
        start_url=str(start_url),
        passcode=str(password) if password else None,
    )


async def update_scheduled_meeting(
    access_token: str,
    meeting_id: str,
    *,
    start_utc: datetime,
    duration_minutes: int,
) -> None:
    """Reschedule an existing Zoom meeting."""
    payload = {
        "start_time": _format_zoom_time(start_utc),
        "duration": duration_minutes,
        "timezone": "UTC",
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.patch(
                f"{ZOOM_API_BASE}/meetings/{meeting_id}",
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("zoom_meeting_update_http_error", meeting_id=meeting_id, error=str(exc))
        raise AppError(
            "Unable to update the Zoom meeting.",
            code="zoom_meeting_update_failed",
        ) from exc

    if response.status_code >= 400:
        logger.warning(
            "zoom_meeting_update_rejected",
            meeting_id=meeting_id,
            status_code=response.status_code,
            body=response.text[:500],
        )
        raise AppError(
            "Unable to update the Zoom meeting.",
            code="zoom_meeting_update_failed",
        )


async def delete_meeting(access_token: str, meeting_id: str) -> None:
    """Delete a Zoom meeting. 404 is treated as success (already gone)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{ZOOM_API_BASE}/meetings/{meeting_id}",
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("zoom_meeting_delete_http_error", meeting_id=meeting_id, error=str(exc))
        raise AppError(
            "Unable to delete the Zoom meeting.",
            code="zoom_meeting_delete_failed",
        ) from exc

    if response.status_code == 404:
        return
    if response.status_code >= 400:
        logger.warning(
            "zoom_meeting_delete_rejected",
            meeting_id=meeting_id,
            status_code=response.status_code,
            body=response.text[:500],
        )
        raise AppError(
            "Unable to delete the Zoom meeting.",
            code="zoom_meeting_delete_failed",
        )
