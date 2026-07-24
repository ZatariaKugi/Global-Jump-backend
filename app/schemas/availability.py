"""Schemas for advisor availability (PRD §3.6)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator


class WeeklySlotInput(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0=Monday … 6=Sunday")
    start_time: time
    end_time: time
    timezone: str = Field(max_length=50, description="IANA name, e.g. Asia/Karachi")

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001 — zoneinfo raises several types
            raise ValueError(f"Unknown IANA timezone: {v}") from exc
        return v

    @model_validator(mode="after")
    def _end_after_start(self) -> WeeklySlotInput:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class WeeklySlotRead(BaseModel):
    id: uuid.UUID
    weekday: int
    start_time: time
    end_time: time
    timezone: str


class WeeklySlotsUpdate(BaseModel):
    slots: list[WeeklySlotInput]


class OverrideInput(BaseModel):
    """Create an unavailable override. ``is_available`` is not accepted — create always blocks."""

    date: date
    reason: str | None = Field(default=None, max_length=255)
    all_day: bool = Field(
        default=True,
        description="True (default) blocks the whole date; false requires start_time/end_time",
    )
    start_time: time | None = Field(
        default=None, description="Advisor-local start; required when all_day is false"
    )
    end_time: time | None = Field(
        default=None, description="Advisor-local end; required when all_day is false"
    )
    timezone: str | None = Field(
        default=None,
        max_length=50,
        description="IANA name for timed blocks; defaults to advisor weekly timezone or UTC",
    )

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001 — zoneinfo raises several types
            raise ValueError(f"Unknown IANA timezone: {v}") from exc
        return v

    @model_validator(mode="after")
    def _all_day_or_window(self) -> OverrideInput:
        if self.all_day:
            if self.start_time is not None or self.end_time is not None:
                raise ValueError("start_time/end_time must be omitted when all_day is true")
            return self
        if self.start_time is None or self.end_time is None:
            raise ValueError("start_time and end_time are required when all_day is false")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class OverrideRead(BaseModel):
    id: uuid.UUID
    date: date
    is_available: bool
    reason: str | None
    all_day: bool
    start_time: time | None
    end_time: time | None
    timezone: str | None


class FreeSlotRead(BaseModel):
    start_utc: datetime
    end_utc: datetime
