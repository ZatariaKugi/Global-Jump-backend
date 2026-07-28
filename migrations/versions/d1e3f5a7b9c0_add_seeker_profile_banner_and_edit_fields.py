"""Add seeker profile banner and edit-sheet fields.

Revision ID: d1e3f5a7b9c0
Revises: c0d2e4f6a8b0
Create Date: 2026-07-25

banner_url, phone, timezone, preferred_language, about for seeker edit profile.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1e3f5a7b9c0"
down_revision = "c0d2e4f6a8b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("seeker_profiles", sa.Column("banner_url", sa.String(length=500), nullable=True))
    op.add_column("seeker_profiles", sa.Column("phone", sa.String(length=40), nullable=True))
    op.add_column("seeker_profiles", sa.Column("timezone", sa.String(length=50), nullable=True))
    op.add_column(
        "seeker_profiles",
        sa.Column("preferred_language", sa.String(length=100), nullable=True),
    )
    op.add_column("seeker_profiles", sa.Column("about", sa.String(length=2000), nullable=True))


def downgrade() -> None:
    op.drop_column("seeker_profiles", "about")
    op.drop_column("seeker_profiles", "preferred_language")
    op.drop_column("seeker_profiles", "timezone")
    op.drop_column("seeker_profiles", "phone")
    op.drop_column("seeker_profiles", "banner_url")
