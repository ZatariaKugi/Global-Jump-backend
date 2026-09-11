"""Add stable offered-service references to bookings.

Revision ID: d2f4b6c8e0a1
Revises: d1f3a5b7c9e2
Create Date: 2026-09-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f4b6c8e0a1"
down_revision: str | None = "d1f3a5b7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("service_id", sa.Uuid(), nullable=True),
    )
    op.create_index(op.f("ix_bookings_service_id"), "bookings", ["service_id"])
    op.create_foreign_key(
        "fk_bookings_service_id",
        "bookings",
        "advisor_offered_services",
        ["service_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Preserve identity for existing bookings where the advisor still has the
    # same offered-service row. Account for the legacy immigration alias while
    # matching; historical rows with a retired service remain valid bookings
    # but intentionally keep a NULL service_id.
    op.execute(
        """
        UPDATE bookings b
        SET service_id = s.id
        FROM advisor_profiles p
        JOIN advisor_offered_services s ON s.profile_id = p.id
        WHERE b.service_id IS NULL
          AND p.user_id = b.advisor_id
          AND s.service_type = CASE b.service_type
                WHEN 'immigration' THEN 'immigration_specialist'
                ELSE b.service_type
              END
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_bookings_service_id", "bookings", type_="foreignkey")
    op.drop_index(op.f("ix_bookings_service_id"), table_name="bookings")
    op.drop_column("bookings", "service_id")
