"""Add regulatory_updates table.

Revision ID: a1b3c5d7e9f0
Revises: a1b2c3d4e5f7
Create Date: 2026-07-28

Admin-curated regulatory / policy feed shown on the advisor dashboard.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b3c5d7e9f0"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "regulatory_updates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("region_label", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), server_default="false", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_regulatory_updates_country_code"),
        "regulatory_updates",
        ["country_code"],
    )
    op.create_index(
        op.f("ix_regulatory_updates_published_at"),
        "regulatory_updates",
        ["published_at"],
    )
    op.create_index(
        op.f("ix_regulatory_updates_is_archived"),
        "regulatory_updates",
        ["is_archived"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_regulatory_updates_is_archived"), table_name="regulatory_updates")
    op.drop_index(op.f("ix_regulatory_updates_published_at"), table_name="regulatory_updates")
    op.drop_index(op.f("ix_regulatory_updates_country_code"), table_name="regulatory_updates")
    op.drop_table("regulatory_updates")
