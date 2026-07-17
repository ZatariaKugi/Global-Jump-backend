"""Canonical visa-type enum (PRD §3.3) — closed set used across the platform."""

from __future__ import annotations

from enum import StrEnum


class VisaType(StrEnum):
    """Advisor specializations / seeker intent / assessment visa types.

    PRD §3.3: tourist, work, student, PR, family, investment, asylum.
    Stored and exchanged as these lowercase values only (``pr`` for PR).
    """

    tourist = "tourist"
    work = "work"
    student = "student"
    pr = "pr"
    family = "family"
    investment = "investment"
    asylum = "asylum"
