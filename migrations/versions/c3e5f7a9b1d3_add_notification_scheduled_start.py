"""Add scheduled_start to notifications.

Revision ID: c3e5f7a9b1d3
Revises: b2d4f6a8c0e1
Create Date: 2026-09-14

Booking-status notifications used to bake a fixed, UTC-labeled clock string
into `body` (see booking_service._booking_summary), so every recipient saw
the identical text regardless of their own timezone. This carries the raw
instant separately so the client can render it in the viewer's own timezone
instead, the way the booking/calendar screens already do.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3e5f7a9b1d3"
down_revision = "b2d4f6a8c0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notifications", "scheduled_start")
