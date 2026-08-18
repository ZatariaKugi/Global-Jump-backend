"""add_support_ticket_booking_snapshot

Link support tickets to a booking and snapshot its session context
(reference, other-party name/type, scheduled start, service type) so the
FE never has to join bookings and history survives booking mutation/deletion.

Revision ID: c3d5e7f9a1b3
Revises: b2c3d4e5f6a7
Create Date: 2026-08-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d5e7f9a1b3"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "support_tickets",
        sa.Column("booking_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "support_tickets",
        sa.Column("booking_reference", sa.String(50), nullable=True),
    )
    op.add_column(
        "support_tickets",
        sa.Column("related_user_name", sa.String(255), nullable=True),
    )
    op.add_column(
        "support_tickets",
        sa.Column("related_user_type", sa.String(20), nullable=True),
    )
    op.add_column(
        "support_tickets",
        sa.Column("session_scheduled_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "support_tickets",
        sa.Column("service_type", sa.String(100), nullable=True),
    )
    op.create_index(
        "ix_support_tickets_booking_id", "support_tickets", ["booking_id"]
    )
    op.create_foreign_key(
        "fk_support_tickets_booking_id_bookings",
        "support_tickets",
        "bookings",
        ["booking_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_support_tickets_booking_id_bookings", "support_tickets", type_="foreignkey"
    )
    op.drop_index("ix_support_tickets_booking_id", table_name="support_tickets")
    op.drop_column("support_tickets", "service_type")
    op.drop_column("support_tickets", "session_scheduled_start")
    op.drop_column("support_tickets", "related_user_type")
    op.drop_column("support_tickets", "related_user_name")
    op.drop_column("support_tickets", "booking_reference")
    op.drop_column("support_tickets", "booking_id")
