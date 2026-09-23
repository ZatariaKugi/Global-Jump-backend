"""Replace service_type identity with service_id and name snapshots.

The catalog row owns the stable service UUID. Advisor offerings, seeker
preferences, bookings, payments and support snapshots use that UUID; ``name``
is retained only as the display/history value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c4e6f2b1d9"
down_revision: str | None = "d2f4b6c8e0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The previous revision called this relation catalog_id. Rename it before
    # backfilling the rest of the application-facing UUID fields.
    op.alter_column(
        "advisor_offered_services", "catalog_id", new_column_name="service_id"
    )
    op.drop_index(
        op.f("ix_advisor_offered_services_catalog_id"),
        table_name="advisor_offered_services",
    )
    op.create_index(
        op.f("ix_advisor_offered_services_service_id"),
        "advisor_offered_services",
        ["service_id"],
    )
    op.execute(
        """
        UPDATE advisor_offered_services catalog
        SET name = catalog.service_type
        WHERE catalog.name IS NULL
        """
    )
    op.execute(
        """
        UPDATE advisor_offered_services offering
        SET name = catalog.name
        FROM advisor_offered_services catalog
        WHERE offering.profile_id IS NOT NULL
          AND offering.service_id = catalog.id
          AND offering.name IS NULL
        """
    )
    op.alter_column(
        "advisor_offered_services",
        "name",
        existing_type=sa.String(100),
        nullable=False,
    )
    # Convert booking identity to the global catalog row and preserve its
    # historical display value in the new name column.
    op.add_column("bookings", sa.Column("name", sa.String(100), nullable=True))
    op.execute("UPDATE bookings SET name = service_type WHERE name IS NULL")
    op.execute(
        """
        UPDATE bookings b
        SET service_id = offering.service_id
        FROM advisor_offered_services offering
        WHERE b.service_id = offering.id
          AND offering.profile_id IS NOT NULL
        """
    )
    op.alter_column(
        "bookings", "name", existing_type=sa.String(100), nullable=False
    )
    op.drop_column("bookings", "service_type")

    # Seeker selections are stored as catalog UUIDs.
    op.add_column("seeker_needed_services", sa.Column("service_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_seeker_needed_services_service_id",
        "seeker_needed_services",
        ["service_id"],
    )
    op.create_foreign_key(
        "fk_seeker_needed_services_service_id",
        "seeker_needed_services",
        "advisor_offered_services",
        ["service_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        UPDATE seeker_needed_services needed
        SET service_id = catalog.id
        FROM advisor_offered_services catalog
        WHERE catalog.profile_id IS NULL
          AND catalog.service_type = needed.service_type
        """
    )
    op.execute("DELETE FROM seeker_needed_services WHERE service_id IS NULL")
    op.alter_column("seeker_needed_services", "service_id", nullable=False)
    op.drop_column("seeker_needed_services", "service_type")

    # The legacy catalog key is no longer needed after all data backfills have
    # used it. Replace its uniqueness rules before removing the column.
    op.drop_constraint(
        "uq_offered_service_profile_type",
        "advisor_offered_services",
        type_="unique",
    )
    op.drop_index("uq_offered_service_catalog_type", table_name="advisor_offered_services")
    op.create_unique_constraint(
        "uq_offered_service_profile_service",
        "advisor_offered_services",
        ["profile_id", "service_id"],
    )
    op.create_index(
        "uq_offered_service_catalog_name",
        "advisor_offered_services",
        ["name"],
        unique=True,
        postgresql_where=sa.text("profile_id IS NULL"),
    )
    op.drop_column("advisor_offered_services", "service_type")

    # Support tickets keep both the stable link and a display snapshot.
    op.add_column("support_tickets", sa.Column("service_id", sa.Uuid(), nullable=True))
    op.add_column("support_tickets", sa.Column("name", sa.String(100), nullable=True))
    op.create_index("ix_support_tickets_service_id", "support_tickets", ["service_id"])
    op.create_foreign_key(
        "fk_support_tickets_service_id",
        "support_tickets",
        "advisor_offered_services",
        ["service_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE support_tickets ticket
        SET service_id = booking.service_id,
            name = booking.name
        FROM bookings booking
        WHERE ticket.booking_id = booking.id
        """
    )
    op.execute("UPDATE support_tickets SET name = service_type WHERE name IS NULL")
    op.drop_column("support_tickets", "service_type")

    # The old normalized table was folded into advisor_offered_services by
    # d1f3a5b7c9e2 and is no longer part of the runtime schema.
    op.drop_index("ix_advisor_services_profile_id", table_name="advisor_services")
    op.drop_table("advisor_services")


def downgrade() -> None:
    # Downgrade is intentionally data-preserving for display values; UUID-only
    # references that cannot be represented by the old schema are restored as
    # the stored name.
    op.add_column("support_tickets", sa.Column("service_type", sa.String(100), nullable=True))
    op.execute("UPDATE support_tickets SET service_type = name")
    op.drop_constraint("fk_support_tickets_service_id", "support_tickets", type_="foreignkey")
    op.drop_index("ix_support_tickets_service_id", table_name="support_tickets")
    op.drop_column("support_tickets", "name")
    op.drop_column("support_tickets", "service_id")

    op.add_column(
        "seeker_needed_services", sa.Column("service_type", sa.String(100), nullable=True)
    )
    op.execute(
        """
        UPDATE seeker_needed_services needed
        SET service_type = catalog.name
        FROM advisor_offered_services catalog
        WHERE needed.service_id = catalog.id
        """
    )
    op.drop_constraint(
        "fk_seeker_needed_services_service_id",
        "seeker_needed_services",
        type_="foreignkey",
    )
    op.drop_index("ix_seeker_needed_services_service_id", table_name="seeker_needed_services")
    op.drop_column("seeker_needed_services", "service_id")
    op.alter_column("seeker_needed_services", "service_type", nullable=False)

    op.add_column("bookings", sa.Column("service_type", sa.String(100), nullable=True))
    op.execute("UPDATE bookings SET service_type = name")
    op.drop_column("bookings", "name")
    op.add_column(
        "advisor_offered_services",
        sa.Column("service_type", sa.String(100), nullable=True),
    )
    op.execute("UPDATE advisor_offered_services SET service_type = name")
    op.drop_index("uq_offered_service_catalog_name", table_name="advisor_offered_services")
    op.drop_constraint(
        "uq_offered_service_profile_service",
        "advisor_offered_services",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_offered_service_profile_type",
        "advisor_offered_services",
        ["profile_id", "service_type"],
    )
    op.create_index(
        "uq_offered_service_catalog_type",
        "advisor_offered_services",
        ["service_type"],
        unique=True,
        postgresql_where=sa.text("profile_id IS NULL"),
    )
    op.alter_column(
        "advisor_offered_services", "service_id", new_column_name="catalog_id"
    )
    op.drop_index(
        op.f("ix_advisor_offered_services_service_id"),
        table_name="advisor_offered_services",
    )
    op.create_index(
        op.f("ix_advisor_offered_services_catalog_id"),
        "advisor_offered_services",
        ["catalog_id"],
    )
