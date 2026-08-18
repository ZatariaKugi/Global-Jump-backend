"""add seeker_profiles.application_submitted_at

Revision ID: e4f6a8b0c2d1
Revises: d3e5f7a9b1c2
Create Date: 2026-08-14 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f6a8b0c2d1"
down_revision: str | None = "d3e5f7a9b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "seeker_profiles",
        sa.Column("application_submitted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("seeker_profiles", "application_submitted_at")
