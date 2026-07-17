"""client_bridge_onboarding_booking_fields

Revision ID: d8f0a2b4c6e8
Revises: e7f9a1b3c5d7
Create Date: 2026-07-17 23:55:00.000000

Bridges schema that exists on cyngro/dev via f1a2/a8f3 but was skipped on this
fork (b2c4 was parented directly to c8d5). Safe to re-run via inspector checks.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f0a2b4c6e8"
down_revision: str | None = "e7f9a1b3c5d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPOINTMENT_NUMBER_START = 3_520_000_000


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    profile_cols = {c["name"] for c in inspector.get_columns("advisor_profiles")}
    booking_cols = {c["name"] for c in inspector.get_columns("bookings")}
    tables = set(inspector.get_table_names())

    if "country_of_residence" not in profile_cols:
        op.add_column(
            "advisor_profiles",
            sa.Column("country_of_residence", sa.String(length=2), nullable=True),
        )
    if "expertise_description" not in profile_cols:
        op.add_column(
            "advisor_profiles",
            sa.Column("expertise_description", sa.String(length=2000), nullable=True),
        )

    if "advisor_offered_services" not in tables:
        op.create_table(
            "advisor_offered_services",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("profile_id", sa.Uuid(), nullable=False),
            sa.Column("service_type", sa.String(length=50), nullable=False),
            sa.ForeignKeyConstraint(["profile_id"], ["advisor_profiles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_advisor_offered_services_profile_id"),
            "advisor_offered_services",
            ["profile_id"],
            unique=False,
        )

    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                "ALTER TYPE document_type ADD VALUE IF NOT EXISTS 'license'; "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        )

    if "appointment_number" not in booking_cols:
        op.add_column(
            "bookings",
            sa.Column("appointment_number", sa.BigInteger(), nullable=True),
        )
        rows = bind.execute(sa.text("SELECT id FROM bookings ORDER BY created_at ASC")).fetchall()
        for offset, (booking_id,) in enumerate(rows):
            bind.execute(
                sa.text("UPDATE bookings SET appointment_number = :num WHERE id = :id"),
                {"num": _APPOINTMENT_NUMBER_START + offset, "id": booking_id},
            )
        op.alter_column("bookings", "appointment_number", nullable=False)
        op.create_unique_constraint(
            "uq_bookings_appointment_number", "bookings", ["appointment_number"]
        )

    if "deal_later_at" not in booking_cols:
        op.add_column(
            "bookings",
            sa.Column("deal_later_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    booking_cols = {c["name"] for c in inspector.get_columns("bookings")}
    profile_cols = {c["name"] for c in inspector.get_columns("advisor_profiles")}
    tables = set(inspector.get_table_names())

    if "deal_later_at" in booking_cols:
        op.drop_column("bookings", "deal_later_at")
    if "appointment_number" in booking_cols:
        op.drop_constraint("uq_bookings_appointment_number", "bookings", type_="unique")
        op.drop_column("bookings", "appointment_number")
    if "advisor_offered_services" in tables:
        op.drop_index(
            op.f("ix_advisor_offered_services_profile_id"),
            table_name="advisor_offered_services",
        )
        op.drop_table("advisor_offered_services")
    if "expertise_description" in profile_cols:
        op.drop_column("advisor_profiles", "expertise_description")
    if "country_of_residence" in profile_cols:
        op.drop_column("advisor_profiles", "country_of_residence")
