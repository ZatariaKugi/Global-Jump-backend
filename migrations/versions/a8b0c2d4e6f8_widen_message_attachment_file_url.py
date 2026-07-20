"""Widen message_attachments.file_url for safe key storage + legacy URLs.

Revision ID: a8b0c2d4e6f8
Revises: f7a9b1c3d5e7
Create Date: 2026-07-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a8b0c2d4e6f8"
down_revision = "f7a9b1c3d5e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "message_attachments",
        "file_url",
        existing_type=sa.String(length=500),
        type_=sa.String(length=2048),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "message_attachments",
        "file_url",
        existing_type=sa.String(length=2048),
        type_=sa.String(length=500),
        existing_nullable=False,
    )
