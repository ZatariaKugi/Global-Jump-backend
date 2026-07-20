"""Add advisor_profiles.banner_url for profile cover images.

Revision ID: b9c1d3e5f7a9
Revises: a8b0c2d4e6f8
Create Date: 2026-07-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b9c1d3e5f7a9"
down_revision = "a8b0c2d4e6f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "advisor_profiles",
        sa.Column("banner_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("advisor_profiles", "banner_url")
