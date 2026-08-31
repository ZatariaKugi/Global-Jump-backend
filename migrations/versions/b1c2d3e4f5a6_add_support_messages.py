"""add support_messages table

Revision ID: b1c2d3e4f5a6
Revises: f7a9c2b4d6e8
Create Date: 2026-08-28
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "8290a70ee081"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "support_messages",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("message", sa.String(5000), nullable=False),
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


def downgrade() -> None:
    op.drop_table("support_messages")
