"""Add price_usd and duration_minutes to advisor_offered_services."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7e9a1b3d5f7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "advisor_offered_services",
        sa.Column("price_usd", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "advisor_offered_services",
        sa.Column(
            "duration_minutes",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )


def downgrade() -> None:
    op.drop_column("advisor_offered_services", "duration_minutes")
    op.drop_column("advisor_offered_services", "price_usd")
