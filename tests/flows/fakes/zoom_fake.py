"""Zoom without Zoom: the OAuth exchange and the meeting API answer locally and are recorded."""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any

from app.services import booking_meeting_service, zoom_meeting_service, zoom_oauth_service
from app.services.zoom_meeting_service import ZoomMeetingInfo
from app.services.zoom_oauth_service import ZoomTokenSet, ZoomUserInfo


class FakeZoom:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.meetings: dict[str, dict[str, Any]] = {}
        self.last_meeting_id: str | None = None
        self._seq = itertools.count(1)

    def _rec(self, name: str, **kw: Any) -> None:
        self.calls.append((name, kw))

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    async def exchange_code(self, code: str, settings: Any) -> ZoomTokenSet:
        self._rec("exchange_code", code=code)
        return ZoomTokenSet(
            access_token=f"zat_{code}",
            refresh_token=f"zrt_{code}",
            expires_in=3600,
            scope="meeting:write user:read",
        )

    async def refresh_access_token(self, refresh_token: str, settings: Any) -> ZoomTokenSet:
        self._rec("refresh_access_token", refresh_token=refresh_token)
        return ZoomTokenSet(
            access_token=f"zat_refreshed_{next(self._seq)}",
            refresh_token=refresh_token,
            expires_in=3600,
            scope="meeting:write user:read",
        )

    async def fetch_zoom_user(self, access_token: str) -> ZoomUserInfo:
        self._rec("fetch_zoom_user", access_token=access_token)
        return ZoomUserInfo(
            zoom_user_id="zoom-user-flows",
            zoom_account_id="zoom-account-flows",
            zoom_email="advisor@zoom.test",
        )

    async def revoke_token(self, token: str, settings: Any) -> None:
        self._rec("revoke_token", token=token)

    async def create_scheduled_meeting(
        self,
        access_token: str,
        *,
        topic: str,
        start_utc: datetime,
        duration_minutes: int,
        **kw: Any,
    ) -> ZoomMeetingInfo:
        mid = f"9{next(self._seq):010d}"
        self.last_meeting_id = mid
        self._rec(
            "create_scheduled_meeting",
            topic=topic,
            start_utc=start_utc,
            duration_minutes=duration_minutes,
            **kw,
        )
        self.meetings[mid] = {
            "topic": topic,
            "start_utc": start_utc,
            "duration_minutes": duration_minutes,
        }
        return ZoomMeetingInfo(
            meeting_id=mid,
            join_url=f"https://zoom.test/j/{mid}",
            start_url=f"https://zoom.test/s/{mid}",
            passcode="123456",
        )

    async def update_scheduled_meeting(
        self,
        access_token: str,
        meeting_id: str,
        *,
        start_utc: datetime,
        duration_minutes: int,
        **kw: Any,
    ) -> None:
        self._rec(
            "update_scheduled_meeting",
            meeting_id=meeting_id,
            start_utc=start_utc,
            duration_minutes=duration_minutes,
            **kw,
        )
        self.meetings[meeting_id].update(
            {"start_utc": start_utc, "duration_minutes": duration_minutes}
        )

    async def delete_meeting(self, access_token: str, meeting_id: str) -> None:
        self._rec("delete_meeting", meeting_id=meeting_id)
        self.meetings.pop(meeting_id, None)

    def install(self, monkeypatch: Any) -> None:
        for name in ("exchange_code", "refresh_access_token", "fetch_zoom_user", "revoke_token"):
            monkeypatch.setattr(zoom_oauth_service, name, getattr(self, name))
        for name in ("create_scheduled_meeting", "update_scheduled_meeting", "delete_meeting"):
            monkeypatch.setattr(zoom_meeting_service, name, getattr(self, name))
            if hasattr(booking_meeting_service, name):  # imported by name there
                monkeypatch.setattr(booking_meeting_service, name, getattr(self, name))
