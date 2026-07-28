"""add_advisor_profile_banner_url

Revision ID: e6f8a0b2c4d6
Revises: d5e7f9a1b3c5
Create Date: 2026-07-18 02:28:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f8a0b2c4d6"
down_revision: str | None = "d5e7f9a1b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("advisor_profiles", sa.Column("banner_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("advisor_profiles", "banner_url")
