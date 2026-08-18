"""Merge support-ticket booking-snapshot head with Zoom connections head.

Revision ID: d4e6f8a0b2c4
Revises: c3d5e7f9a1b3, c5e7a9b1d3f4
Create Date: 2026-08-13

Both branches diverge from ``b2c3d4e5f6a7`` and touch disjoint tables
(support_tickets vs. zoom_connections/bookings), so ``alembic upgrade head``
has a single tip again with no additional schema changes.
"""

from __future__ import annotations

from alembic import op

revision = "d4e6f8a0b2c4"
down_revision = ("c3d5e7f9a1b3", "c5e7a9b1d3f4")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both parents already applied schema changes on their branches.
    pass


def downgrade() -> None:
    pass
