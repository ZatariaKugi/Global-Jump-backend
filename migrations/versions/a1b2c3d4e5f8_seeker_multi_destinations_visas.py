"""Add multi-value seeker intended destinations and visa types.

Revision ID: a1b2c3d4e5f8
Revises: f0a2b4c6d8e1
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f8"
down_revision = "f0a2b4c6d8e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seeker_intended_destinations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["seeker_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_seeker_intended_destinations_profile_id"),
        "seeker_intended_destinations",
        ["profile_id"],
        unique=False,
    )

    op.create_table(
        "seeker_intended_visa_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("visa_type", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["seeker_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_seeker_intended_visa_types_profile_id"),
        "seeker_intended_visa_types",
        ["profile_id"],
        unique=False,
    )

    # Backfill from legacy singular columns (preserve selection order as single row).
    op.execute(
        """
        INSERT INTO seeker_intended_destinations (id, profile_id, country_code)
        SELECT gen_random_uuid(), id, intended_destination
        FROM seeker_profiles
        WHERE intended_destination IS NOT NULL
          AND intended_destination <> ''
        """
    )
    op.execute(
        """
        INSERT INTO seeker_intended_visa_types (id, profile_id, visa_type)
        SELECT gen_random_uuid(), id, intended_visa_type
        FROM seeker_profiles
        WHERE intended_visa_type IS NOT NULL
          AND intended_visa_type <> ''
        """
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_seeker_intended_visa_types_profile_id"),
        table_name="seeker_intended_visa_types",
    )
    op.drop_table("seeker_intended_visa_types")
    op.drop_index(
        op.f("ix_seeker_intended_destinations_profile_id"),
        table_name="seeker_intended_destinations",
    )
    op.drop_table("seeker_intended_destinations")
