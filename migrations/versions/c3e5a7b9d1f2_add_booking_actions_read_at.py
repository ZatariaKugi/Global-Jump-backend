"""add booking actions read timestamps

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e3
Create Date: 2026-08-31 22:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e5a7b9d1f2"
down_revision: str | None = "b2d4f6a8c0e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("advisor_actions_read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("seeker_actions_read_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bookings", "seeker_actions_read_at")
    op.drop_column("bookings", "advisor_actions_read_at")
