"""add_booking_tables

Revision ID: c9e4a6b1d8f3
Revises: b7d2f4a8c6e1
Create Date: 2026-06-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "c9e4a6b1d8f3"
down_revision: str | None = "b7d2f4a8c6e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOOKING_STATUS = PgEnum(
    "pending",
    "confirmed",
    "completed",
    "cancelled",
    "no_show",
    name="booking_status",
    create_type=False,
)
_PAYMENT_STATUS = PgEnum(
    "unpaid",
    "paid",
    "refunded",
    name="payment_status",
    create_type=False,
)


def _create_enum_safe(name: str, values: str) -> None:
    """Create a PG enum type, silently skipping if it already exists."""
    op.execute(
        sa.text(
            f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({values}); "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )


def upgrade() -> None:
    _create_enum_safe("booking_status", "'pending','confirmed','completed','cancelled','no_show'")
    _create_enum_safe("payment_status", "'unpaid','paid','refunded'")

    op.add_column(
        "advisor_profiles",
        sa.Column(
            "cancellation_notice_hours",
            sa.Integer(),
            server_default="24",
            nullable=False,
        ),
    )

    op.create_table(
        "advisor_weekly_slots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("advisor_id", sa.Uuid(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["advisor_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_advisor_weekly_slots_advisor_id"),
        "advisor_weekly_slots",
        ["advisor_id"],
        unique=False,
    )

    op.create_table(
        "advisor_availability_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("advisor_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["advisor_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_advisor_availability_overrides_advisor_id"),
        "advisor_availability_overrides",
        ["advisor_id"],
        unique=False,
    )

    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("advisor_id", sa.Uuid(), nullable=False),
        sa.Column("service_type", sa.String(length=100), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price_usd", sa.Float(), nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", _BOOKING_STATUS, nullable=False),
        sa.Column("payment_status", _PAYMENT_STATUS, nullable=False),
        sa.Column("cancellation_reason", sa.String(length=500), nullable=True),
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
        sa.Column("customer_note", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["advisor_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bookings_customer_id"), "bookings", ["customer_id"], unique=False)
    op.create_index(op.f("ix_bookings_advisor_id"), "bookings", ["advisor_id"], unique=False)
    op.create_index(op.f("ix_bookings_is_archived"), "bookings", ["is_archived"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_bookings_is_archived"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_advisor_id"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_customer_id"), table_name="bookings")
    op.drop_table("bookings")

    op.drop_index(
        op.f("ix_advisor_availability_overrides_advisor_id"),
        table_name="advisor_availability_overrides",
    )
    op.drop_table("advisor_availability_overrides")

    op.drop_index(op.f("ix_advisor_weekly_slots_advisor_id"), table_name="advisor_weekly_slots")
    op.drop_table("advisor_weekly_slots")

    op.drop_column("advisor_profiles", "cancellation_notice_hours")

    op.execute(sa.text("DROP TYPE IF EXISTS payment_status"))
    op.execute(sa.text("DROP TYPE IF EXISTS booking_status"))
