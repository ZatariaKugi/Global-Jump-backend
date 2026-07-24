"""Add timed fields to availability overrides.

Revision ID: e2f4a6b8c0d1
Revises: d1e3f5a7b9c0
Create Date: 2026-07-25

Nullable start_time/end_time/timezone for partial-day blocks; null times = all-day.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2f4a6b8c0d1"
down_revision = "d1e3f5a7b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "advisor_availability_overrides",
        sa.Column("start_time", sa.Time(), nullable=True),
    )
    op.add_column(
        "advisor_availability_overrides",
        sa.Column("end_time", sa.Time(), nullable=True),
    )
    op.add_column(
        "advisor_availability_overrides",
        sa.Column("timezone", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("advisor_availability_overrides", "timezone")
    op.drop_column("advisor_availability_overrides", "end_time")
    op.drop_column("advisor_availability_overrides", "start_time")
