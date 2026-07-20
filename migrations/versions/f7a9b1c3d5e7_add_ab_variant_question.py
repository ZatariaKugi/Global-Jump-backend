"""Add question column to assessment_ab_variants.

Revision ID: f7a9b1c3d5e7
Revises: e6f8a0b2c4d6
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7a9b1c3d5e7"
down_revision = "d5e7f9a1b3c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assessment_ab_variants",
        sa.Column("question", sa.String(length=500), nullable=True),
    )
    # Backfill from description/name so existing rows stay readable on the panel.
    op.execute(
        sa.text(
            """
            UPDATE assessment_ab_variants
            SET question = COALESCE(
                NULLIF(BTRIM(description), ''),
                NULLIF(BTRIM(name), ''),
                'Untitled experiment'
            )
            WHERE question IS NULL
            """
        )
    )
    op.alter_column(
        "assessment_ab_variants",
        "question",
        existing_type=sa.String(length=500),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("assessment_ab_variants", "question")
