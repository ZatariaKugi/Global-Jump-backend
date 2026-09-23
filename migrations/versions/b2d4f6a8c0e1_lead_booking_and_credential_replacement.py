"""Add replacement-required credential status and explicit lead booking links."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4f6a8c0e1"
down_revision: str | None = "a8c4e6f2b1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE credential_status ADD VALUE IF NOT EXISTS 'replacement_required'"
    )
    op.add_column(
        "advisor_leads",
        sa.Column("booking_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_advisor_leads_booking_id",
        "advisor_leads",
        ["booking_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_advisor_leads_booking_id_bookings",
        "advisor_leads",
        "bookings",
        ["booking_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_advisor_leads_booking_id_bookings",
        "advisor_leads",
        type_="foreignkey",
    )
    op.drop_index("ix_advisor_leads_booking_id", table_name="advisor_leads")
    op.drop_column("advisor_leads", "booking_id")
    # PostgreSQL enum values cannot be removed safely in-place. Existing rows
    # using replacement_required must be migrated before a manual enum rebuild.

