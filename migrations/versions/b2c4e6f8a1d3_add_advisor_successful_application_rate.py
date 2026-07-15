"""add_advisor_successful_application_rate

Revision ID: b2c4e6f8a1d3
Revises: c8d5b56d844d
Create Date: 2026-07-16 01:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c4e6f8a1d3"
down_revision: str | None = "c8d5b56d844d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "advisor_profiles",
        sa.Column("successful_application_rate", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("advisor_profiles", "successful_application_rate")
