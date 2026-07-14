"""Abstract base model shared by every globlejump table."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BaseModel(Base):
    """
    Abstract base for all globlejump tables.

    created_by / updated_by are bare UUIDs — no FK to the identity-service DB.
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, sort_order=-10)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, sort_order=98
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        sort_order=99,
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True, sort_order=100)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True, sort_order=101)

    is_archived: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False, index=True, sort_order=102
    )

    def archive(self, user_id: uuid.UUID) -> None:
        self.is_archived = True
        self.updated_by = user_id

    def unarchive(self, user_id: uuid.UUID) -> None:
        self.is_archived = False
        self.updated_by = user_id
