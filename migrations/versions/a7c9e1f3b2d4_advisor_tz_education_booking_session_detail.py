"""advisor timezone/education + booking session-detail columns

Revision ID: a7c9e1f3b2d4
Revises: 6f95d15d0526
Create Date: 2026-08-07 00:00:00.000000

Adds:
- advisor_profiles.timezone (IANA tz) and advisor_profiles.education (degree text)
- bookings timeline timestamps (paid_at, confirmed_at, completed_at)
- bookings meeting metadata (platform, id, passcode, recording url)

All columns are nullable — no backfill required.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c9e1f3b2d4"
down_revision: str | None = "6f95d15d0526"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "advisor_profiles",
        sa.Column("timezone", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "advisor_profiles",
        sa.Column("education", sa.String(length=200), nullable=True),
    )

    op.add_column(
        "bookings",
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("meeting_platform", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("meeting_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("meeting_passcode", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("meeting_recording_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bookings", "meeting_recording_url")
    op.drop_column("bookings", "meeting_passcode")
    op.drop_column("bookings", "meeting_id")
    op.drop_column("bookings", "meeting_platform")
    op.drop_column("bookings", "completed_at")
    op.drop_column("bookings", "confirmed_at")
    op.drop_column("bookings", "paid_at")
    op.drop_column("advisor_profiles", "education")
    op.drop_column("advisor_profiles", "timezone")
