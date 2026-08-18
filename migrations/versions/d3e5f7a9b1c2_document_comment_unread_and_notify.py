"""document comment unread flag + seeker_document notify entity

Revision ID: d3e5f7a9b1c2
Revises: c2d4e6f8a0b1
Create Date: 2026-08-14 01:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3e5f7a9b1c2"
down_revision: str | None = "c2d4e6f8a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE notification_entity_type ADD VALUE IF NOT EXISTS 'seeker_document'")
    op.add_column(
        "seeker_documents",
        sa.Column("comments_last_read_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("seeker_documents", "comments_last_read_at")
    # PostgreSQL cannot drop an enum value safely.
