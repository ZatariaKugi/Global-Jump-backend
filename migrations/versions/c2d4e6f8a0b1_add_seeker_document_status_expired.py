"""add expired to seeker_document_status

Revision ID: c2d4e6f8a0b1
Revises: b1c3d5e7f9a0
Create Date: 2026-08-14 00:40:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c2d4e6f8a0b1"
down_revision: str | None = "b1c3d5e7f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE seeker_document_status ADD VALUE IF NOT EXISTS 'expired'")


def downgrade() -> None:
    op.execute("UPDATE seeker_documents SET status = 'under_review' WHERE status = 'expired'")
    # PostgreSQL cannot drop an enum value safely.
