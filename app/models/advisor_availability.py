"""Advisor availability calendar — weekly recurring slots and one-off overrides."""

from __future__ import annotations

import uuid
from datetime import date, time

from sqlalchemy import Date, ForeignKey, Integer, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AdvisorWeeklySlot(Base):
    """A recurring weekly availability window in the advisor's local time zone."""

    __tablename__ = "advisor_weekly_slots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Mon … 6=Sun
    start_time: Mapped[time] = mapped_column(Time, nullable=False)  # advisor-local
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False)  # IANA name


class AdvisorAvailabilityOverride(Base):
    """A one-off date the advisor blocks out (whole day)."""

    __tablename__ = "advisor_availability_overrides"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    advisor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    is_available: Mapped[bool] = mapped_column(default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
