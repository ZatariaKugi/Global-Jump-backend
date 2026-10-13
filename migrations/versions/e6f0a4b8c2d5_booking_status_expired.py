"""booking_status_expired

An unpaid booking expires at its scheduled start (QA GJ-BOOKING-003, PM 2026-10-10).

Revision ID: e6f0a4b8c2d5
Revises: d5e9f3a7b1c4
Create Date: 2026-10-10 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f0a4b8c2d5"
down_revision: str | None = "d5e9f3a7b1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE booking_status ADD VALUE IF NOT EXISTS 'expired'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; rows keep the label. Nothing to undo.
    pass
