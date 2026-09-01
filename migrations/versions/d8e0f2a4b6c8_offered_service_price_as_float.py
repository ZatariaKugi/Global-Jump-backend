"""Store offered-service price_usd as float (readable) instead of numeric binary."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8e0f2a4b6c8"
down_revision = "c7e9a1b3d5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "advisor_offered_services",
        "price_usd",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Float(),
        existing_nullable=True,
        postgresql_using="price_usd::double precision",
    )


def downgrade() -> None:
    op.alter_column(
        "advisor_offered_services",
        "price_usd",
        existing_type=sa.Float(),
        type_=sa.Numeric(10, 2),
        existing_nullable=True,
        postgresql_using="price_usd::numeric(10,2)",
    )
