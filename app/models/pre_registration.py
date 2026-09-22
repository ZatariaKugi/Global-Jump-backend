"""Pre-launch interest submissions from the public Pre-Registration form (#309)."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class PreRegistrationInterest(StrEnum):
    seeker = "seeker"
    advisor = "advisor"


class PreRegistration(BaseModel):
    __tablename__ = "pre_registrations"

    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    interest: Mapped[PreRegistrationInterest] = mapped_column(
        SAEnum(PreRegistrationInterest, name="pre_registration_interest"),
        nullable=False,
        index=True,
    )
    message: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
