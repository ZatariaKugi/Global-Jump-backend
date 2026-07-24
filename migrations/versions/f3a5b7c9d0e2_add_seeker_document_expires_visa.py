"""Add expires_at and visa_type to seeker documents.

Revision ID: f3a5b7c9d0e2
Revises: e2f4a6b8c0d1
Create Date: 2026-07-25

Optional expiry date and visa scope for portfolio list/checklist filters.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a5b7c9d0e2"
down_revision = "e2f4a6b8c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "seeker_documents",
        sa.Column("expires_at", sa.Date(), nullable=True),
    )
    op.add_column(
        "seeker_documents",
        sa.Column("visa_type", sa.String(length=50), nullable=True),
    )
    op.create_index(
        op.f("ix_seeker_documents_visa_type"),
        "seeker_documents",
        ["visa_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_seeker_documents_visa_type"), table_name="seeker_documents")
    op.drop_column("seeker_documents", "visa_type")
    op.drop_column("seeker_documents", "expires_at")
