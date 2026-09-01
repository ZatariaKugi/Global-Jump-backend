"""Public support / contact-us messages — no auth required."""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_model import BaseModel


class SupportMessage(BaseModel):
    __tablename__ = "support_messages"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(String(5000), nullable=False)
