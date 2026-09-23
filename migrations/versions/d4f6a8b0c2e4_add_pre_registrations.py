"""add pre_registrations table

Revision ID: d4f6a8b0c2e4
Revises: c3e5f7a9b1d3
Create Date: 2026-09-22
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4f6a8b0c2e4"
down_revision = "c3e5f7a9b1d3"
branch_labels: str | None = None
depends_on: str | None = None

_INTEREST = postgresql.ENUM(
    "seeker", "advisor", name="pre_registration_interest", create_type=False
)


def upgrade() -> None:
    _INTEREST.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "pre_registrations",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column("full_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(30), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("interest", _INTEREST, nullable=False),
        sa.Column("message", sa.String(2000), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "is_archived",
            sa.Boolean(),
            server_default="false",
            nullable=False,
            index=True,
        ),
    )
    op.create_index("ix_pre_registrations_email", "pre_registrations", ["email"])
    op.create_index("ix_pre_registrations_interest", "pre_registrations", ["interest"])


def downgrade() -> None:
    op.drop_index("ix_pre_registrations_interest", table_name="pre_registrations")
    op.drop_index("ix_pre_registrations_email", table_name="pre_registrations")
    op.drop_table("pre_registrations")
    _INTEREST.drop(op.get_bind(), checkfirst=True)
