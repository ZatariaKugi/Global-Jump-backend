"""Record when a seeker first opened AI recommended advisors.

The seeker dashboard's journey chart showed the "AI Recommendation" bar at 100% for a
seeker who had never used AI recommendations, because profile matches were being counted
(PM, 2026-10-09). The bar now follows this timestamp, stamped by
``GET /advisors?recommended=true`` for an entitled seeker.

Revision ID: d5e9f3a7b1c4
Revises: c4d8e2f6a0b3
Create Date: 2026-10-09 03:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e9f3a7b1c4"
down_revision: str | None = "c4d8e2f6a0b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "seeker_profiles",
        sa.Column("ai_recommendations_viewed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("seeker_profiles", "ai_recommendations_viewed_at")
